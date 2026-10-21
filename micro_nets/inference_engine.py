# -*- coding: utf-8 -*-
"""
micro_nets/inference_engine.py
==============================
Детерминированный C11 W8A8 (INT8 Weight x INT8 Activation -> INT32 Accumulator)
движок инференса управляющих микросетей COMPASS-HYBRID v2.6:
1. RouterNet-1.1M
2. DissimulationDropoutNet-1.6M
3. ClusterVerifyNet-0.7M
Все матричные умножения, LayerNorm, GELU и конформные множества Mondrian RAPS
выполняются внутри нативного C11-ядра (c_core/micronets_int8.c).
"""

from __future__ import annotations

import ctypes
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from backend.bridges.bio_native import (
    NativeBioMnetsLib,
    CMicroNetDescriptor,
    CMicroNetConformalOut,
)
from micro_nets.conformal_calibration import MondrianRAPSController, ConformalPredictionResult


@dataclass
class QuantizedLinearLayer:
    """Хранит непрерывные в памяти C-совместимые буферы INT8 весов, масштабов и смещений."""
    weight_int8: np.ndarray        # contiguous int8, shape (out_dim, in_dim)
    scale_per_channel: np.ndarray  # contiguous float32, shape (out_dim,)
    bias_fp32: np.ndarray          # contiguous float32, shape (out_dim,)

    @classmethod
    def quantize_from_fp32(cls, weight: np.ndarray, bias: np.ndarray) -> "QuantizedLinearLayer":
        w = np.ascontiguousarray(weight, dtype=np.float32)
        b = np.ascontiguousarray(bias, dtype=np.float32).ravel()
        max_abs = np.max(np.abs(w), axis=1)
        scales = np.where(max_abs > 1e-8, max_abs / 127.0, 1.0 / 127.0).astype(np.float32)
        w_q = np.clip(np.rint(w / scales[:, np.newaxis]), -127, 127).astype(np.int8)
        return cls(
            weight_int8=np.ascontiguousarray(w_q, dtype=np.int8),
            scale_per_channel=np.ascontiguousarray(scales, dtype=np.float32),
            bias_fp32=np.ascontiguousarray(b, dtype=np.float32),
        )


class QuantizedResidualMicroNet:
    """
    Микросеть, исполняемая нативным C11 W8A8 ядром через дескриптор CMicroNetDescriptor.
    """

    def __init__(
        self,
        name: str,
        input_dim: int,
        hidden_dim: int,
        num_layers: int,
        output_dim: int,
        class_labels: Sequence[str],
        critical_indices: Sequence[int],
        seed: int = 42,
    ) -> None:
        self.name = str(name)
        self.input_dim = int(input_dim)
        self.hidden_dim = int(hidden_dim)
        self.num_layers = int(num_layers)
        self.output_dim = int(output_dim)
        self.class_labels = list(class_labels)
        self.critical_indices = list(critical_indices)
        self._clib = NativeBioMnetsLib.get().lib

        self.input_mean = np.zeros(self.input_dim, dtype=np.float32)
        self.input_std = np.ones(self.input_dim, dtype=np.float32)

        self.stem_layer: Optional[QuantizedLinearLayer] = None
        self.res_blocks: List[Tuple[QuantizedLinearLayer, QuantizedLinearLayer]] = []
        self.head_layer: Optional[QuantizedLinearLayer] = None

        self.conformal_controller = MondrianRAPSController(
            network_name=self.name,
            class_labels=self.class_labels,
            critical_class_indices=self.critical_indices,
        )
        self._c_descriptor = CMicroNetDescriptor()
        self._init_deterministic_weights(seed=seed)

    def _init_deterministic_weights(self, seed: int) -> None:
        rng = np.random.default_rng(seed)
        w_stem = rng.normal(0.0, np.sqrt(2.0 / self.input_dim), size=(self.hidden_dim, self.input_dim))
        b_stem = np.zeros(self.hidden_dim, dtype=np.float32)
        self.stem_layer = QuantizedLinearLayer.quantize_from_fp32(w_stem, b_stem)

        self.res_blocks = []
        for _ in range(self.num_layers):
            w1 = rng.normal(0.0, np.sqrt(2.0 / self.hidden_dim), size=(self.hidden_dim, self.hidden_dim))
            b1 = np.zeros(self.hidden_dim, dtype=np.float32)
            w2 = rng.normal(0.0, np.sqrt(1.0 / self.hidden_dim), size=(self.hidden_dim, self.hidden_dim))
            b2 = np.zeros(self.hidden_dim, dtype=np.float32)
            self.res_blocks.append((
                QuantizedLinearLayer.quantize_from_fp32(w1, b1),
                QuantizedLinearLayer.quantize_from_fp32(w2, b2),
            ))

        w_head = rng.normal(0.0, np.sqrt(1.0 / self.hidden_dim), size=(self.output_dim, self.hidden_dim))
        b_head = np.zeros(self.output_dim, dtype=np.float32)
        self.head_layer = QuantizedLinearLayer.quantize_from_fp32(w_head, b_head)
        self.sync_c_descriptor()

    def sync_c_descriptor(self) -> None:
        """Синхронизирует указатели C-структуры CMicroNetDescriptor с буферами NumPy."""
        assert self.stem_layer is not None and self.head_layer is not None
        desc = self._c_descriptor
        desc.hidden_dim = self.hidden_dim
        desc.num_blocks = self.num_layers

        for i in range(24):
            desc.input_mean[i] = float(self.input_mean[i])
            desc.input_std[i] = float(max(self.input_std[i], 1e-5))

        desc.stem_w_int8 = self.stem_layer.weight_int8.ctypes.data_as(ctypes.POINTER(ctypes.c_int8))
        desc.stem_scale = self.stem_layer.scale_per_channel.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        desc.stem_bias = self.stem_layer.bias_fp32.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        for b_idx in range(self.num_layers):
            fc1, fc2 = self.res_blocks[b_idx]
            desc.block_fc1_w_int8[b_idx] = fc1.weight_int8.ctypes.data_as(ctypes.POINTER(ctypes.c_int8))
            desc.block_fc1_scale[b_idx] = fc1.scale_per_channel.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            desc.block_fc1_bias[b_idx] = fc1.bias_fp32.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

            desc.block_fc2_w_int8[b_idx] = fc2.weight_int8.ctypes.data_as(ctypes.POINTER(ctypes.c_int8))
            desc.block_fc2_scale[b_idx] = fc2.scale_per_channel.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            desc.block_fc2_bias[b_idx] = fc2.bias_fp32.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        desc.head_w_int8 = self.head_layer.weight_int8.ctypes.data_as(ctypes.POINTER(ctypes.c_int8))
        desc.head_scale = self.head_layer.scale_per_channel.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        desc.head_bias = self.head_layer.bias_fp32.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        desc.temperature = float(self.conformal_controller.temperature)
        desc.lambda_reg = float(self.conformal_controller.lambda_reg)
        desc.k_reg = int(self.conformal_controller.k_reg)

        for c_idx in range(3):
            desc.class_quantiles[c_idx] = float(
                self.conformal_controller.class_quantiles.get(c_idx, self.conformal_controller.global_quantile)
            )

        crit_mask = 0
        for c_idx in self.critical_indices:
            crit_mask |= (1 << int(c_idx))
        desc.critical_class_bitmask = ctypes.c_uint32(crit_mask)

    def forward_hidden_batch_via_c11(self, X_batch: np.ndarray) -> np.ndarray:
        """
        Вычисляет скрытые представления предпоследнего слоя через C11 W8A8 ядро
        compass_w8a8_gemv (для точного совпадения дистилляции и инференса).
        """
        assert self.stem_layer is not None
        X = np.ascontiguousarray(X_batch, dtype=np.float32)
        if X.ndim == 1:
            X = X[np.newaxis, :]
        n_samples = X.shape[0]
        H_out = np.zeros((n_samples, self.hidden_dim), dtype=np.float32)

        h_buf = np.zeros(self.hidden_dim, dtype=np.float32)
        h_mid = np.zeros(self.hidden_dim, dtype=np.float32)
        h_fc2 = np.zeros(self.hidden_dim, dtype=np.float32)

        for idx in range(n_samples):
            norm_x = np.ascontiguousarray(
                (X[idx] - self.input_mean) / np.maximum(self.input_std, 1e-5), dtype=np.float32
            )
            self._clib.compass_w8a8_gemv(
                self._c_descriptor.stem_w_int8,
                self._c_descriptor.stem_scale,
                self._c_descriptor.stem_bias,
                norm_x.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                ctypes.c_int32(self.hidden_dim),
                ctypes.c_int32(self.input_dim),
                h_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            )
            h_buf[:] = 0.5 * h_buf * (1.0 + np.tanh(0.7978845608 * (h_buf + 0.044715 * (h_buf ** 3))))

            for b_idx in range(self.num_layers):
                mean = np.mean(h_buf)
                std = np.sqrt(np.var(h_buf) + 1e-6)
                h_norm = np.ascontiguousarray((h_buf - mean) / std, dtype=np.float32)

                self._clib.compass_w8a8_gemv(
                    self._c_descriptor.block_fc1_w_int8[b_idx],
                    self._c_descriptor.block_fc1_scale[b_idx],
                    self._c_descriptor.block_fc1_bias[b_idx],
                    h_norm.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    ctypes.c_int32(self.hidden_dim),
                    ctypes.c_int32(self.hidden_dim),
                    h_mid.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                )
                h_mid[:] = 0.5 * h_mid * (1.0 + np.tanh(0.7978845608 * (h_mid + 0.044715 * (h_mid ** 3))))

                self._clib.compass_w8a8_gemv(
                    self._c_descriptor.block_fc2_w_int8[b_idx],
                    self._c_descriptor.block_fc2_scale[b_idx],
                    self._c_descriptor.block_fc2_bias[b_idx],
                    h_mid.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    ctypes.c_int32(self.hidden_dim),
                    ctypes.c_int32(self.hidden_dim),
                    h_fc2.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                )
                h_buf += 0.5 * h_fc2

            mean_f = np.mean(h_buf)
            std_f = np.sqrt(np.var(h_buf) + 1e-6)
            H_out[idx] = (h_buf - mean_f) / std_f

        return H_out

    def forward_logits(self, x: np.ndarray) -> np.ndarray:
        """Вычисляет выходные логиты через C11 W8A8 тракт."""
        assert self.head_layer is not None
        single = (x.ndim == 1)
        H = self.forward_hidden_batch_via_c11(x)
        logits = np.zeros((H.shape[0], self.output_dim), dtype=np.float32)
        out_row = np.zeros(self.output_dim, dtype=np.float32)

        for i in range(H.shape[0]):
            h_row = np.ascontiguousarray(H[i], dtype=np.float32)
            self._clib.compass_w8a8_gemv(
                self._c_descriptor.head_w_int8,
                self._c_descriptor.head_scale,
                self._c_descriptor.head_bias,
                h_row.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                ctypes.c_int32(self.output_dim),
                ctypes.c_int32(self.hidden_dim),
                out_row.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            )
            logits[i] = out_row

        return logits[0] if single else logits

    def update_head_from_distilled_fp32(
        self,
        w_stem: np.ndarray,
        b_stem: np.ndarray,
        w_head: np.ndarray,
        b_head: np.ndarray,
        input_mean: np.ndarray,
        input_std: np.ndarray,
    ) -> None:
        self.input_mean = np.ascontiguousarray(input_mean, dtype=np.float32).ravel()
        self.input_std = np.ascontiguousarray(np.maximum(input_std, 1e-5), dtype=np.float32).ravel()
        self.stem_layer = QuantizedLinearLayer.quantize_from_fp32(w_stem, b_stem)
        self.head_layer = QuantizedLinearLayer.quantize_from_fp32(w_head, b_head)
        self.sync_c_descriptor()

    def infer_conformal(self, feature_vector: np.ndarray) -> ConformalPredictionResult:
        """
        Выполняет весь инференс и конформную проверку Mondrian RAPS за один C11-вызов
        compass_micronet_forward_raps.
        """
        x_c = np.ascontiguousarray(np.asarray(feature_vector, dtype=np.float32).ravel()[:24])
        c_out = CMicroNetConformalOut()
        rc = self._clib.compass_micronet_forward_raps(
            ctypes.byref(self._c_descriptor),
            x_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            ctypes.byref(c_out),
        )
        if rc != 0:
            raise RuntimeError(f"C11 compass_micronet_forward_raps failed with code {rc}")

        bitmask = int(c_out.prediction_set_bitmask)
        pred_indices = [i for i in range(3) if (bitmask & (1 << i)) != 0]
        pred_labels = [self.class_labels[i] for i in pred_indices]
        point_idx = int(c_out.point_prediction_idx)
        min_alpha = min(
            self.conformal_controller.class_alphas.get(i, self.conformal_controller.default_alpha)
            for i in pred_indices
        )

        return ConformalPredictionResult(
            network_name=self.name,
            point_prediction_idx=point_idx,
            point_prediction_label=self.class_labels[point_idx],
            point_confidence=round(float(c_out.point_confidence), 6),
            prediction_set_indices=pred_indices,
            prediction_set_labels=pred_labels,
            set_size=int(c_out.prediction_set_size),
            nonconformity_scores=[round(float(c_out.raps_scores[i]), 6) for i in range(3)],
            shannon_entropy_nats=round(float(c_out.shannon_entropy_nats), 6),
            is_ambiguous_or_ood=bool(c_out.is_ambiguous_or_ood),
            mandatory_escalation=bool(c_out.mandatory_escalation),
            guaranteed_coverage_target=round(1.0 - min_alpha, 4),
        )


@dataclass(frozen=True)
class ControlPlaneDecision:
    router_decision: ConformalPredictionResult
    dropout_dissimulation_decision: ConformalPredictionResult
    cluster_verify_decision: ConformalPredictionResult
    final_execution_route: str
    escalate_to_heavy_verifier: bool
    escalation_reasons: List[str]
    total_inference_latency_ms: float

    def to_dict(self) -> Dict:
        return asdict(self)


class CompassControlPlaneEngine:
    """Единый оркестратор трех C11 W8A8 микросетей."""

    FEATURE_DIM: int = 24

    def __init__(self, artifacts_dir: Optional[Union[str, Path]] = None) -> None:
        if artifacts_dir is None:
            artifacts_dir = Path(__file__).resolve().parent / "artifacts"
        self.artifacts_dir = Path(artifacts_dir)

        self.router_net = QuantizedResidualMicroNet(
            name="RouterNet-1.1M",
            input_dim=self.FEATURE_DIM,
            hidden_dim=192,
            num_layers=3,
            output_dim=3,
            class_labels=[
                "FAST_SAFE_PATH",
                "CLINICAL_CBR_CLUSTER_PATH",
                "HARD_STOP_DEEP_VERIFICATION",
            ],
            critical_indices=[2],
            seed=101,
        )
        self.dissimulation_net = QuantizedResidualMicroNet(
            name="DissimulationDropoutNet-1.6M",
            input_dim=self.FEATURE_DIM,
            hidden_dim=224,
            num_layers=3,
            output_dim=3,
            class_labels=[
                "ADHERENT_CONCORDANT",
                "MODERATE_DROPOUT_RISK",
                "HIGH_RISK_MASKED_DISSIMULATION",
            ],
            critical_indices=[2],
            seed=202,
        )
        self.cluster_verify_net = QuantizedResidualMicroNet(
            name="ClusterVerifyNet-0.7M",
            input_dim=self.FEATURE_DIM,
            hidden_dim=160,
            num_layers=2,
            output_dim=3,
            class_labels=[
                "IN_CLUSTER_HIGH_CONFIDENCE",
                "BOUNDARY_TRANSITION_PHENOTYPE",
                "OUT_OF_DISTRIBUTION_ANOMALY",
            ],
            critical_indices=[2],
            seed=303,
        )
        self.load_artifacts_if_available()

    def save_artifacts(self) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        meta_path = self.artifacts_dir / "conformal_control_plane_meta.json"
        weights_path = self.artifacts_dir / "quantized_control_plane_int8.npz"

        for net in (self.router_net, self.dissimulation_net, self.cluster_verify_net):
            net.sync_c_descriptor()

        meta_payload = {
            "router_net": self.router_net.conformal_controller.to_state_dict(),
            "dissimulation_net": self.dissimulation_net.conformal_controller.to_state_dict(),
            "cluster_verify_net": self.cluster_verify_net.conformal_controller.to_state_dict(),
        }
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(meta_payload, fh, ensure_ascii=False, indent=2)

        np_dict = {}
        for prefix, net in (
            ("router", self.router_net),
            ("dissim", self.dissimulation_net),
            ("cluster", self.cluster_verify_net),
        ):
            assert net.stem_layer is not None and net.head_layer is not None
            np_dict[f"{prefix}_input_mean"] = net.input_mean
            np_dict[f"{prefix}_input_std"] = net.input_std
            np_dict[f"{prefix}_stem_w"] = net.stem_layer.weight_int8
            np_dict[f"{prefix}_stem_s"] = net.stem_layer.scale_per_channel
            np_dict[f"{prefix}_stem_b"] = net.stem_layer.bias_fp32
            np_dict[f"{prefix}_head_w"] = net.head_layer.weight_int8
            np_dict[f"{prefix}_head_s"] = net.head_layer.scale_per_channel
            np_dict[f"{prefix}_head_b"] = net.head_layer.bias_fp32

        np.savez_compressed(weights_path, **np_dict)
        return meta_path

    def load_artifacts_if_available(self) -> bool:
        meta_path = self.artifacts_dir / "conformal_control_plane_meta.json"
        weights_path = self.artifacts_dir / "quantized_control_plane_int8.npz"
        if not (meta_path.exists() and weights_path.exists()):
            return False

        try:
            with meta_path.open("r", encoding="utf-8") as fh:
                meta = json.load(fh)
            self.router_net.conformal_controller = MondrianRAPSController.from_state_dict(meta["router_net"])
            self.dissimulation_net.conformal_controller = MondrianRAPSController.from_state_dict(meta["dissimulation_net"])
            self.cluster_verify_net.conformal_controller = MondrianRAPSController.from_state_dict(meta["cluster_verify_net"])

            data = np.load(weights_path)
            for prefix, net in (
                ("router", self.router_net),
                ("dissim", self.dissimulation_net),
                ("cluster", self.cluster_verify_net),
            ):
                net.input_mean = np.ascontiguousarray(data[f"{prefix}_input_mean"], dtype=np.float32)
                net.input_std = np.ascontiguousarray(data[f"{prefix}_input_std"], dtype=np.float32)
                net.stem_layer = QuantizedLinearLayer(
                    weight_int8=np.ascontiguousarray(data[f"{prefix}_stem_w"], dtype=np.int8),
                    scale_per_channel=np.ascontiguousarray(data[f"{prefix}_stem_s"], dtype=np.float32),
                    bias_fp32=np.ascontiguousarray(data[f"{prefix}_stem_b"], dtype=np.float32),
                )
                net.head_layer = QuantizedLinearLayer(
                    weight_int8=np.ascontiguousarray(data[f"{prefix}_head_w"], dtype=np.int8),
                    scale_per_channel=np.ascontiguousarray(data[f"{prefix}_head_s"], dtype=np.float32),
                    bias_fp32=np.ascontiguousarray(data[f"{prefix}_head_b"], dtype=np.float32),
                )
                net.sync_c_descriptor()
            return True
        except Exception:
            return False

    def evaluate_patient_vector(self, vector_24d: Sequence[float]) -> ControlPlaneDecision:
        t0 = time.perf_counter()
        x = np.asarray(vector_24d, dtype=np.float32).ravel()
        if x.size < self.FEATURE_DIM:
            x = np.pad(x, (0, self.FEATURE_DIM - x.size))
        elif x.size > self.FEATURE_DIM:
            x = x[: self.FEATURE_DIM]

        res_router = self.router_net.infer_conformal(x)
        res_dissim = self.dissimulation_net.infer_conformal(x)
        res_cluster = self.cluster_verify_net.infer_conformal(x)

        reasons: List[str] = []
        if res_router.mandatory_escalation:
            reasons.append(f"RouterNet C11-RAPS set={res_router.prediction_set_labels}")
        if res_dissim.mandatory_escalation:
            reasons.append(f"DissimulationNet C11-RAPS set={res_dissim.prediction_set_labels}")
        if res_cluster.mandatory_escalation:
            reasons.append(f"ClusterVerifyNet C11-RAPS set={res_cluster.prediction_set_labels}")

        escalate = len(reasons) > 0
        if escalate:
            final_route = "TIER_3_FULL_VERIFICATION_AND_XAI_AUDIT"
        elif (
            res_router.point_prediction_idx == 1
            or res_dissim.point_prediction_idx == 1
            or res_cluster.point_prediction_idx == 1
        ):
            final_route = "TIER_2_TRAJECTORY_CBR_CLUSTER_ANALYSIS"
        else:
            final_route = "TIER_1_DETERMINISTIC_FAST_TRACK"

        latency_ms = (time.perf_counter() - t0) * 1000.0
        return ControlPlaneDecision(
            router_decision=res_router,
            dropout_dissimulation_decision=res_dissim,
            cluster_verify_decision=res_cluster,
            final_execution_route=final_route,
            escalate_to_heavy_verifier=escalate,
            escalation_reasons=reasons,
            total_inference_latency_ms=round(float(latency_ms), 4),
        )