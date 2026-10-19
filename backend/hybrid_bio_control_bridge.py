# -*- coding: utf-8 -*-
"""
backend/hybrid_bio_control_bridge.py
====================================
Связующий мост между нативным C11 акустическим DSP + W2-адаптированным
Демпстером-Шефером (День 5) и C11 W8A8 Control Plane из трех микросетей (micro_nets/).
Готов к прямому вызову из backend/api_router_bio.py и backend/safe_pipeline.py.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from backend.bio_acoustic_dsp import NonlinearAcousticDSP, AcousticBiomarkerVector
from backend.bio_evidential_fusion import DempsterShaferBioFusion, EvidentialFusionReport
from micro_nets.inference_engine import CompassControlPlaneEngine, ControlPlaneDecision
from micro_nets.train_distill_manifold import run_full_control_plane_distillation


class HybridBioControlBridge:
    """
    Высокоуровневый координатор, объединяющий русскоязычный C11 био-контур
    и C11 W8A8 микросети управления.
    """

    def __init__(self) -> None:
        self.dsp = NonlinearAcousticDSP()
        self.fusion = DempsterShaferBioFusion()
        self.control_plane = CompassControlPlaneEngine()
        if not self.control_plane.router_net.conformal_controller.is_calibrated:
            run_full_control_plane_distillation()
            self.control_plane.load_artifacts_if_available()

    def process_clinical_encounter(
        self,
        hama_total: float,
        madrs_total: float,
        patient_self_report_wellbeing: float,
        cyp2d6_activity_score: float = 1.5,
        cyp3a4_inhibition_factor: float = 1.0,
        egfr_norm: float = 0.9,
        alt_ast_ratio: float = 0.5,
        qtc_delta_norm: float = 0.1,
        maoi_washout_violation: float = 0.0,
        adverse_event_markers_count: int = 0,
        hard_stop_triggered: bool = False,
        pcm_audio: Optional[np.ndarray] = None,
        sample_rate: int = 16000,
        telemetry_summary: Optional[Dict[str, float]] = None,
        raw_kintsugi_dep_score: float = -1.0,
        raw_kintsugi_anx_score: float = -1.0,
        response_latency_sec: float = 1.6,
        mahalanobis_cluster_dist: float = 0.35,
    ) -> Dict:
        """
        Выполняет сквозной расчет в C11:
        1. Акустический DSP (LPC-4 глоттальный фильтр + TKEO + русский бимодальный Вейбулл).
        2. W2-адаптация Kintsugi DAM и слияние свидетельств Демпстера-Шефера.
        3. Сборка 24-мерного вектора состояния и конформный вывод 3 микросетей (C11 W8A8 + RAPS).
        """
        if pcm_audio is not None:
            biomarkers: AcousticBiomarkerVector = self.dsp.extract_from_pcm(
                pcm_audio, sample_rate=sample_rate
            )
        else:
            biomarkers = self.dsp.build_from_telemetry_summary(telemetry_summary or {})

        evidential_report: EvidentialFusionReport = self.fusion.evaluate_patient_multimodal(
            biomarkers=biomarkers,
            hama_total=hama_total,
            madrs_total=madrs_total,
            patient_self_report_wellbeing=patient_self_report_wellbeing,
            raw_kintsugi_dep_score=raw_kintsugi_dep_score,
            raw_kintsugi_anx_score=raw_kintsugi_anx_score,
            adverse_event_markers_count=adverse_event_markers_count,
            cyp_inhibition_factor=cyp3a4_inhibition_factor,
            response_latency_sec=response_latency_sec,
        )

        hama_norm = float(np.clip(hama_total / 30.0, 0.0, 1.8))
        madrs_norm = float(np.clip(madrs_total / 34.0, 0.0, 1.8))

        vector_24d = np.array([
            hama_norm,
            madrs_norm,
            0.5 * (hama_norm + madrs_norm),
            float(np.clip(patient_self_report_wellbeing, 0.0, 1.0)),
            float(cyp2d6_activity_score),
            float(cyp3a4_inhibition_factor),
            float(egfr_norm),
            float(alt_ast_ratio),
            float(qtc_delta_norm),
            float(maoi_washout_violation),
            0.45,
            0.25,
            biomarkers.glottal_tkeo_instability,
            biomarkers.jitter_ppq5_pct,
            biomarkers.shimmer_apq5_pct,
            biomarkers.weibull_pause_shape_k,
            biomarkers.weibull_pause_scale_lambda_ms / 1000.0,
            biomarkers.cognitive_pause_fraction,
            evidential_report.dempster_conflict_k,
            evidential_report.dissimulation_index,
            float(response_latency_sec) / 5.0,
            0.0,
            float(mahalanobis_cluster_dist),
            1.0 if hard_stop_triggered else 0.0,
        ], dtype=np.float32)

        control_decision: ControlPlaneDecision = self.control_plane.evaluate_patient_vector(vector_24d)

        return {
            "acoustic_biomarkers": biomarkers.to_dict(),
            "evidential_dempster_shafer": evidential_report.to_dict(),
            "control_plane_micronets": control_decision.to_dict(),
        }