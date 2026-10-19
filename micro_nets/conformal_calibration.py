# -*- coding: utf-8 -*-
"""
micro_nets/conformal_calibration.py
===================================
Конформный контроль риска (Mondrian Regularized Adaptive Prediction Sets — RAPS)
для управляющих микросетей COMPASS-HYBRID v2.6 (RouterNet, DissimulationDropoutNet, ClusterVerifyNet).

Обеспечивает неасимптотическую (finite-sample) гарантию покрытия:
    P(Y_true in C(X_test) | Y_true = y) >= 1 - alpha_y
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence, Union

import numpy as np

_EPS: float = 1e-12


@dataclass(frozen=True)
class ConformalPredictionResult:
    """Результат конформного вывода для одного клинического кейса."""
    network_name: str
    point_prediction_idx: int
    point_prediction_label: str
    point_confidence: float
    prediction_set_indices: List[int]
    prediction_set_labels: List[str]
    set_size: int
    nonconformity_scores: List[float]
    shannon_entropy_nats: float
    is_ambiguous_or_ood: bool
    mandatory_escalation: bool
    guaranteed_coverage_target: float

    def to_dict(self) -> Dict:
        return asdict(self)


class MondrianRAPSController:
    """
    Реализует Mondrian Split-Conformal RAPS калибровку с индивидуальными
    бюджетами ошибок alpha_y для каждого клинического класса.
    """

    def __init__(
        self,
        network_name: str,
        class_labels: Sequence[str],
        class_alphas: Optional[Dict[int, float]] = None,
        default_alpha: float = 0.02,
        lambda_reg: float = 0.05,
        k_reg: int = 1,
        critical_class_indices: Optional[Sequence[int]] = None,
    ) -> None:
        if len(class_labels) < 2:
            raise ValueError("Число классов для конформного контроллера должно быть >= 2.")
        self.network_name = str(network_name)
        self.class_labels = list(class_labels)
        self.num_classes = len(self.class_labels)
        self.default_alpha = float(np.clip(default_alpha, 0.001, 0.5))
        self.lambda_reg = float(max(0.0, lambda_reg))
        self.k_reg = int(max(1, k_reg))
        self.critical_class_indices = set(
            critical_class_indices if critical_class_indices is not None else [self.num_classes - 1]
        )

        self.class_alphas: Dict[int, float] = {}
        for idx in range(self.num_classes):
            if class_alphas and idx in class_alphas:
                self.class_alphas[idx] = float(np.clip(class_alphas[idx], 0.0005, 0.5))
            elif idx in self.critical_class_indices:
                self.class_alphas[idx] = min(self.default_alpha, 0.005)
            else:
                self.class_alphas[idx] = self.default_alpha

        self.class_quantiles: Dict[int, float] = {i: 1.0 for i in range(self.num_classes)}
        self.global_quantile: float = 1.0
        self.temperature: float = 1.0
        self.is_calibrated: bool = False
        self.calibration_sample_sizes: Dict[int, int] = {i: 0 for i in range(self.num_classes)}

    @staticmethod
    def softmax_with_temperature(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
        t = max(float(temperature), 1e-3)
        z = np.asarray(logits, dtype=np.float64) / t
        if z.ndim == 1:
            z_shifted = z - np.max(z)
            exp_z = np.exp(z_shifted)
            return exp_z / (np.sum(exp_z) + _EPS)
        z_shifted = z - np.max(z, axis=-1, keepdims=True)
        exp_z = np.exp(z_shifted)
        return exp_z / (np.sum(exp_z, axis=-1, keepdims=True) + _EPS)

    def optimize_temperature_nll(
        self,
        cal_logits: np.ndarray,
        cal_labels: np.ndarray,
        grid_steps: int = 60,
    ) -> float:
        logits = np.asarray(cal_logits, dtype=np.float64)
        labels = np.asarray(cal_labels, dtype=np.int64).ravel()
        if logits.shape[0] != labels.size or labels.size == 0:
            raise ValueError("Размерности cal_logits и cal_labels не совпадают или пусты.")

        t_candidates = np.logspace(-0.7, 0.7, num=grid_steps)
        best_t = 1.0
        best_nll = float("inf")
        row_idx = np.arange(labels.size)

        for t_val in t_candidates:
            probs = self.softmax_with_temperature(logits, temperature=float(t_val))
            true_probs = np.clip(probs[row_idx, labels], _EPS, 1.0)
            nll = float(-np.mean(np.log(true_probs)))
            if nll < best_nll:
                best_nll = nll
                best_t = float(t_val)

        self.temperature = best_t
        return best_t

    def compute_raps_scores_matrix(self, probs: np.ndarray) -> np.ndarray:
        p = np.asarray(probs, dtype=np.float64)
        single_input = (p.ndim == 1)
        if single_input:
            p = p[np.newaxis, :]

        n_samples, n_classes = p.shape
        sorted_indices = np.argsort(-p, axis=1)
        sorted_probs = np.take_along_axis(p, sorted_indices, axis=1)
        cumulative_probs = np.cumsum(sorted_probs, axis=1)

        ranks_sorted = np.arange(1, n_classes + 1, dtype=np.float64)[np.newaxis, :]
        reg_penalty_sorted = self.lambda_reg * np.maximum(0.0, ranks_sorted - float(self.k_reg))
        scores_sorted = cumulative_probs + reg_penalty_sorted

        scores = np.empty_like(scores_sorted)
        row_idx = np.arange(n_samples)[:, np.newaxis]
        scores[row_idx, sorted_indices] = scores_sorted

        return scores[0] if single_input else scores

    @staticmethod
    def _finite_sample_conformal_quantile(scores: np.ndarray, alpha: float) -> float:
        s = np.sort(np.asarray(scores, dtype=np.float64).ravel())
        n = s.size
        if n == 0:
            return 1.0
        q_level = math.ceil((n + 1) * (1.0 - float(alpha))) / float(n)
        if q_level >= 1.0:
            return float(s[-1] + 1e-4)
        idx = max(0, min(n - 1, int(math.ceil(q_level * n)) - 1))
        return float(s[idx])

    def fit_calibration(
        self,
        cal_logits: np.ndarray,
        cal_labels: np.ndarray,
        tune_temperature: bool = True,
    ) -> Dict[str, Union[float, Dict[int, float]]]:
        logits = np.asarray(cal_logits, dtype=np.float64)
        labels = np.asarray(cal_labels, dtype=np.int64).ravel()
        if logits.shape[0] != labels.size or labels.size < 4:
            raise ValueError("Для конформной калибровки требуется минимум 4 размеченных примера.")

        if tune_temperature:
            self.optimize_temperature_nll(logits, labels)

        probs = self.softmax_with_temperature(logits, self.temperature)
        all_scores_matrix = self.compute_raps_scores_matrix(probs)
        row_idx = np.arange(labels.size)
        true_class_scores = all_scores_matrix[row_idx, labels]

        self.global_quantile = self._finite_sample_conformal_quantile(
            true_class_scores, self.default_alpha
        )

        for cls_idx in range(self.num_classes):
            cls_mask = (labels == cls_idx)
            cls_scores = true_class_scores[cls_mask]
            self.calibration_sample_sizes[cls_idx] = int(cls_scores.size)
            alpha_y = self.class_alphas[cls_idx]

            if cls_scores.size >= 3:
                q_cls = self._finite_sample_conformal_quantile(cls_scores, alpha_y)
                if cls_idx in self.critical_class_indices:
                    q_gw = self._finite_sample_conformal_quantile(true_class_scores, alpha_y)
                    q_cls = max(q_cls, q_gw)
                self.class_quantiles[cls_idx] = q_cls
            else:
                self.class_quantiles[cls_idx] = self._finite_sample_conformal_quantile(
                    true_class_scores, alpha_y
                )

        self.is_calibrated = True
        return {
            "temperature": round(self.temperature, 6),
            "global_quantile": round(self.global_quantile, 6),
            "class_quantiles": {k: round(v, 6) for k, v in self.class_quantiles.items()},
        }

    def to_state_dict(self) -> Dict:
        return {
            "network_name": self.network_name,
            "class_labels": self.class_labels,
            "default_alpha": self.default_alpha,
            "lambda_reg": self.lambda_reg,
            "k_reg": self.k_reg,
            "critical_class_indices": sorted(list(self.critical_class_indices)),
            "class_alphas": {str(k): v for k, v in self.class_alphas.items()},
            "class_quantiles": {str(k): v for k, v in self.class_quantiles.items()},
            "global_quantile": self.global_quantile,
            "temperature": self.temperature,
            "is_calibrated": self.is_calibrated,
            "calibration_sample_sizes": {str(k): v for k, v in self.calibration_sample_sizes.items()},
        }

    @classmethod
    def from_state_dict(cls, state: Dict) -> "MondrianRAPSController":
        ctrl = cls(
            network_name=state["network_name"],
            class_labels=state["class_labels"],
            class_alphas={int(k): float(v) for k, v in state.get("class_alphas", {}).items()},
            default_alpha=float(state.get("default_alpha", 0.02)),
            lambda_reg=float(state.get("lambda_reg", 0.05)),
            k_reg=int(state.get("k_reg", 1)),
            critical_class_indices=state.get("critical_class_indices", []),
        )
        ctrl.class_quantiles = {int(k): float(v) for k, v in state.get("class_quantiles", {}).items()}
        ctrl.global_quantile = float(state.get("global_quantile", 1.0))
        ctrl.temperature = float(state.get("temperature", 1.0))
        ctrl.is_calibrated = bool(state.get("is_calibrated", True))
        ctrl.calibration_sample_sizes = {
            int(k): int(v) for k, v in state.get("calibration_sample_sizes", {}).items()
        }
        return ctrl