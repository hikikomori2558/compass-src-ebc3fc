# -*- coding: utf-8 -*-
"""
backend/bridges/ot_xai.py
==========================
Строго типизированный ctypes-мост к нативным C11-модулям Варианта 4
(c_core/ot_xai_mahalanobis.c):
1. Дебиасированная дивергенция Синкхорна-Вассерштейна S_eps(alpha, beta) для
   нерегулярных 36-недельных траекторий.
2. Аналитический сжимающий оцениватель ковариации Ледуа-Вульфа + OOD-детектор
   Махаланобиса в 24-мерном фенотипическом пространстве.
3. Точные двухуровневые коалиционные значения Оуэна (Owen 1977) по 6 физиологическим
   блокам и генератор минимальных контрафактических вмешательств.
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np


FEATURE_NAMES_24D: List[str] = [
    "hama_norm",
    "madrs_norm",
    "cgis_norm",
    "self_report_wellbeing",
    "cyp2d6_activity_score",
    "cyp3a4_inhibition_factor",
    "egfr_norm",
    "alt_ast_ratio",
    "qtc_delta_norm",
    "maoi_washout_violation",
    "age_norm",
    "prior_ssri_failures",
    "glottal_tkeo_instability",
    "jitter_ppq5_pct",
    "shimmer_apq5_pct",
    "weibull_pause_shape_k",
    "weibull_pause_scale_s",
    "cognitive_pause_fraction",
    "dempster_conflict_k",
    "dissimulation_index",
    "response_latency_norm",
    "trajectory_slope_4w",
    "mahalanobis_cluster_dist",
    "hard_stop_rule_active",
]

UNION_NAMES_6: List[str] = [
    "U0_Psychometric_Severity",
    "U1_Pharmacogenetics_CYP_DDI",
    "U2_Hepatorenal_Cardiac_Safety",
    "U3_Glottal_Acoustic_Biomarkers",
    "U4_Evidential_Dissimulation_DST",
    "U5_Longitudinal_Geometry_HardStop",
]

DEFAULT_HEALTHY_BASELINE_24D = np.array([
    0.18, 0.16, 0.17, 0.88,  # U0: Низкие HAM-A/MADRS, высокое благополучие
    1.25, 1.00, 0.95, 0.40,  # CYP2D6 NM, без ингибиторов CYP3A4, норма СКФ и АЛТ/АСТ
    0.08, 0.00, 0.40, 0.00,  # Норма QTc, нет ИМАО
    0.17, 0.42, 2.05, 1.25,  # U3: Здоровая глоттальная просодика
    0.30, 0.15, 0.05, 0.04,  # U4: Низкий конфликт Демпстера и диссимуляция
    0.30, -0.15, 0.25, 0.00  # U5: Положительная динамика, внутри кластера, без Hard-Stop
], dtype=np.float64)


from backend.bridges.loader import resolve_bridge_lib


class CSinkhornTrajectoryResult(ctypes.Structure):
    _fields_ = [
        ("ot_epsilon_ab", ctypes.c_double),
        ("ot_self_aa", ctypes.c_double),
        ("ot_self_bb", ctypes.c_double),
        ("sinkhorn_divergence", ctypes.c_double),
        ("wasserstein_barycentric_shift", ctypes.c_double),
        ("sinkhorn_iterations", ctypes.c_int32),
        ("transport_plan", ctypes.c_double * (64 * 64)),
    ]


class CLedoitWolfClusterModel(ctypes.Structure):
    _fields_ = [
        ("cluster_id", ctypes.c_int32),
        ("n_samples", ctypes.c_int32),
        ("shrinkage_rho", ctypes.c_double),
        ("trace_mu", ctypes.c_double),
        ("condition_number_raw", ctypes.c_double),
        ("condition_number_shrunk", ctypes.c_double),
        ("log_det_sigma", ctypes.c_double),
        ("centroid", ctypes.c_double * 24),
        ("precision_matrix", ctypes.c_double * (24 * 24)),
        ("cholesky_l", ctypes.c_double * (24 * 24)),
    ]


class CMahalanobisOODResult(ctypes.Structure):
    _fields_ = [
        ("best_cluster_id", ctypes.c_int32),
        ("mahalanobis_sq", ctypes.c_double),
        ("mahalanobis_dist", ctypes.c_double),
        ("chi2_p_value", ctypes.c_double),
        ("chi2_threshold_99", ctypes.c_double),
        ("is_ood_anomaly", ctypes.c_int32),
        ("top_contributing_axes", ctypes.c_double * 24),
    ]


class COwenXAIResult(ctypes.Structure):
    _fields_ = [
        ("baseline_risk_v0", ctypes.c_double),
        ("patient_risk_vn", ctypes.c_double),
        ("owen_phi", ctypes.c_double * 24),
        ("union_attribution", ctypes.c_double * 6),
        ("efficiency_residual", ctypes.c_double),
        ("exact_coalition_evals", ctypes.c_int32),
        ("counterfactual_vector", ctypes.c_double * 24),
        ("counterfactual_deltas", ctypes.c_double * 24),
        ("counterfactual_risk_after", ctypes.c_double),
        ("relative_risk_reduction_pct", ctypes.c_double),
        ("modified_features_count", ctypes.c_int32),
    ]


def ensure_c11_ot_xai_compiled() -> Path:
    """
    Компилирует c_core/ot_xai_mahalanobis.c в разделяемую библиотеку
    libcompass_ot_xai.(so|dylib) с флагами -O3 -std=c11 -fPIC -lm.
    """
    repo_root = Path(__file__).resolve().parent.parent
    c_dir = repo_root / "c_core"
    ext = "dylib" if platform.system() == "Darwin" else "so"
    lib_path = c_dir / f"libcompass_ot_xai.{ext}"

    src_file = c_dir / "ot_xai_mahalanobis.c"
    hdr_file = c_dir / "ot_xai_mahalanobis.h"

    need_build = not lib_path.exists()
    if not need_build:
        lib_mtime = lib_path.stat().st_mtime
        if src_file.stat().st_mtime > lib_mtime or hdr_file.stat().st_mtime > lib_mtime:
            need_build = True

    if need_build:
        cmd = [
            "cc",
            "-O3",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-fPIC",
            "-shared",
            str(src_file),
            "-o",
            str(lib_path),
            "-lm",
        ]
        subprocess.run(cmd, check=True)

    return lib_path


class NativeOTXAILib:
    """Синглтон-обертка над скомпилированной C11 библиотекой libcompass_ot_xai."""
    _instance: Optional["NativeOTXAILib"] = None

    def __init__(self) -> None:
# Приоритет: общий libcompass_core.so (build_c_core собирает все c_core/*.c разом);
# компилятор в рантайме не нужен — multi-stage-образ собирается без gcc.
        lib_path = str(resolve_bridge_lib("libcompass_ot_xai", ('compass_sinkhorn_trajectory_divergence', 'compass_fit_ledoit_wolf_cluster_24d', 'compass_evaluate_mahalanobis_ood_24d', 'compass_compute_exact_owen_and_counterfactual')))
        self.lib = ctypes.CDLL(str(lib_path))
        self._bind_signatures()

    @classmethod
    def get(cls) -> "NativeOTXAILib":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _bind_signatures(self) -> None:
        self.lib.compass_sinkhorn_trajectory_divergence.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.POINTER(CSinkhornTrajectoryResult),
        ]
        self.lib.compass_sinkhorn_trajectory_divergence.restype = ctypes.c_int32

        self.lib.compass_fit_ledoit_wolf_cluster_24d.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.POINTER(CLedoitWolfClusterModel),
        ]
        self.lib.compass_fit_ledoit_wolf_cluster_24d.restype = ctypes.c_int32

        self.lib.compass_evaluate_mahalanobis_ood_24d.argtypes = [
            ctypes.POINTER(CLedoitWolfClusterModel),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(CMahalanobisOODResult),
        ]
        self.lib.compass_evaluate_mahalanobis_ood_24d.restype = ctypes.c_int32

        self.lib.compass_compute_exact_owen_and_counterfactual.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_double,
            ctypes.POINTER(COwenXAIResult),
        ]
        self.lib.compass_compute_exact_owen_and_counterfactual.restype = ctypes.c_int32


@dataclass(frozen=True)
class SinkhornDivergenceReport:
    ot_epsilon_ab: float
    ot_self_aa: float
    ot_self_bb: float
    sinkhorn_divergence: float
    wasserstein_barycentric_shift: float
    sinkhorn_iterations: int

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class MahalanobisOODReport:
    best_cluster_id: int
    mahalanobis_sq: float
    mahalanobis_dist: float
    chi2_p_value: float
    chi2_threshold_99: float
    is_ood_anomaly: bool
    shrinkage_rho: float
    condition_number_raw: float
    condition_number_shrunk: float
    feature_contributions_to_d2: Dict[str, float]

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class OwenCounterfactualReport:
    baseline_risk_v0: float
    patient_risk_vn: float
    owen_values_24d: Dict[str, float]
    union_attributions_6: Dict[str, float]
    efficiency_axiom_residual: float
    exact_coalition_evaluations: int
    counterfactual_recommendations: Dict[str, Dict[str, float]]
    counterfactual_risk_after: float
    relative_risk_reduction_pct: float
    modified_features_count: int

    def to_dict(self) -> Dict:
        return asdict(self)


class C11TrajectoryOTAndXAIOrchestrator:
    """
    Высокоуровневый научный оркестратор Варианта 4 для интеграции
    в backend/domain/trajectory.py и backend/domain/evidence_xai.py.
    """

    def __init__(self) -> None:
        self._clib = NativeOTXAILib.get().lib
        self._cluster_models: Dict[int, CLedoitWolfClusterModel] = {}

    def compute_trajectory_sinkhorn_divergence(
        self,
        patient_weeks: Sequence[float],
        patient_states: np.ndarray,
        ref_weeks: Sequence[float],
        ref_states: np.ndarray,
        epsilon_reg: float = 0.08,
        lambda_time_weight: float = 0.50,
    ) -> SinkhornDivergenceReport:
        t_pat = np.ascontiguousarray(patient_weeks, dtype=np.float64).ravel()
        s_pat = np.ascontiguousarray(patient_states, dtype=np.float64)
        if s_pat.ndim == 1:
            s_pat = s_pat[:, np.newaxis]

        t_ref = np.ascontiguousarray(ref_weeks, dtype=np.float64).ravel()
        s_ref = np.ascontiguousarray(ref_states, dtype=np.float64)
        if s_ref.ndim == 1:
            s_ref = s_ref[:, np.newaxis]

        n_pat, state_dim = s_pat.shape
        n_ref, ref_dim = s_ref.shape
        if state_dim != ref_dim:
            raise ValueError(f"Размерности состояний траекторий не совпадают: {state_dim} vs {ref_dim}")

        c_out = CSinkhornTrajectoryResult()
        rc = self._clib.compass_sinkhorn_trajectory_divergence(
            t_pat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            s_pat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            None,
            ctypes.c_int32(n_pat),
            t_ref.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            s_ref.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            None,
            ctypes.c_int32(n_ref),
            ctypes.c_int32(state_dim),
            ctypes.c_double(epsilon_reg),
            ctypes.c_double(lambda_time_weight),
            ctypes.byref(c_out),
        )
        if rc != 0:
            raise RuntimeError(f"compass_sinkhorn_trajectory_divergence failed with code {rc}")

        return SinkhornDivergenceReport(
            ot_epsilon_ab=round(float(c_out.ot_epsilon_ab), 8),
            ot_self_aa=round(float(c_out.ot_self_aa), 8),
            ot_self_bb=round(float(c_out.ot_self_bb), 8),
            sinkhorn_divergence=round(float(c_out.sinkhorn_divergence), 8),
            wasserstein_barycentric_shift=round(float(c_out.wasserstein_barycentric_shift), 6),
            sinkhorn_iterations=int(c_out.sinkhorn_iterations),
        )

    def fit_cluster_ledoit_wolf(
        self,
        samples_24d: np.ndarray,
        cluster_id: int = 0,
    ) -> CLedoitWolfClusterModel:
        X = np.ascontiguousarray(samples_24d, dtype=np.float64)
        if X.ndim != 2 or X.shape[1] != 24:
            raise ValueError("Матрица наблюдений для кластера должна иметь размерность (N, 24).")

        model = CLedoitWolfClusterModel()
        rc = self._clib.compass_fit_ledoit_wolf_cluster_24d(
            X.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_int32(X.shape[0]),
            ctypes.c_int32(cluster_id),
            ctypes.byref(model),
        )
        if rc != 0:
            raise RuntimeError(f"compass_fit_ledoit_wolf_cluster_24d failed with code {rc}")

        self._cluster_models[cluster_id] = model
        return model

    def evaluate_mahalanobis_ood(
        self,
        patient_vec_24d: Sequence[float],
        cluster_id: int = 0,
    ) -> MahalanobisOODReport:
        if cluster_id not in self._cluster_models:
            raise KeyError(f"Кластер {cluster_id} еще не обучен через fit_cluster_ledoit_wolf.")

        model = self._cluster_models[cluster_id]
        x = np.ascontiguousarray(patient_vec_24d, dtype=np.float64).ravel()[:24]
        c_ood = CMahalanobisOODResult()

        rc = self._clib.compass_evaluate_mahalanobis_ood_24d(
            ctypes.byref(model),
            x.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.byref(c_ood),
        )
        if rc != 0:
            raise RuntimeError(f"compass_evaluate_mahalanobis_ood_24d failed with code {rc}")

        contribs = {
            FEATURE_NAMES_24D[i]: round(float(c_ood.top_contributing_axes[i]), 6)
            for i in range(24)
        }

        return MahalanobisOODReport(
            best_cluster_id=int(c_ood.best_cluster_id),
            mahalanobis_sq=round(float(c_ood.mahalanobis_sq), 6),
            mahalanobis_dist=round(float(c_ood.mahalanobis_dist), 6),
            chi2_p_value=round(float(c_ood.chi2_p_value), 8),
            chi2_threshold_99=round(float(c_ood.chi2_threshold_99), 4),
            is_ood_anomaly=bool(c_ood.is_ood_anomaly),
            shrinkage_rho=round(float(model.shrinkage_rho), 6),
            condition_number_raw=round(float(model.condition_number_raw), 2),
            condition_number_shrunk=round(float(model.condition_number_shrunk), 2),
            feature_contributions_to_d2=contribs,
        )

    def explain_with_exact_owen_and_counterfactual(
        self,
        patient_vec_24d: Sequence[float],
        healthy_baseline_24d: Optional[Sequence[float]] = None,
        target_safe_risk_threshold: float = 28.0,
    ) -> OwenCounterfactualReport:
        x_pat = np.ascontiguousarray(patient_vec_24d, dtype=np.float64).ravel()[:24]
        x_base = np.ascontiguousarray(
            healthy_baseline_24d if healthy_baseline_24d is not None else DEFAULT_HEALTHY_BASELINE_24D,
            dtype=np.float64,
        ).ravel()[:24]

        c_xai = COwenXAIResult()
        rc = self._clib.compass_compute_exact_owen_and_counterfactual(
            x_pat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            x_base.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_double(target_safe_risk_threshold),
            ctypes.byref(c_xai),
        )
        if rc != 0:
            raise RuntimeError(f"compass_compute_exact_owen_and_counterfactual failed with code {rc}")

        owen_dict = {
            FEATURE_NAMES_24D[i]: round(float(c_xai.owen_phi[i]), 6)
            for i in range(24)
        }
        union_dict = {
            UNION_NAMES_6[u]: round(float(c_xai.union_attribution[u]), 6)
            for u in range(6)
        }

        cf_recs: Dict[str, Dict[str, float]] = {}
        for i in range(24):
            delta = float(c_xai.counterfactual_deltas[i])
            if abs(delta) > 1e-5:
                cf_recs[FEATURE_NAMES_24D[i]] = {
                    "observed_value": round(float(x_pat[i]), 4),
                    "recommended_target": round(float(c_xai.counterfactual_vector[i]), 4),
                    "intervention_delta": round(delta, 4),
                    "owen_risk_contribution": round(float(c_xai.owen_phi[i]), 4),
                }

        return OwenCounterfactualReport(
            baseline_risk_v0=round(float(c_xai.baseline_risk_v0), 6),
            patient_risk_vn=round(float(c_xai.patient_risk_vn), 6),
            owen_values_24d=owen_dict,
            union_attributions_6=union_dict,
            efficiency_axiom_residual=float(c_xai.efficiency_residual),
            exact_coalition_evaluations=int(c_xai.exact_coalition_evals),
            counterfactual_recommendations=cf_recs,
            counterfactual_risk_after=round(float(c_xai.counterfactual_risk_after), 6),
            relative_risk_reduction_pct=round(float(c_xai.relative_risk_reduction_pct), 2),
            modified_features_count=int(c_xai.modified_features_count),
        )