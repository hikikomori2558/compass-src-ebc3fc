# -*- coding: utf-8 -*-
"""
backend/bridges/bio_fusion.py
================================
Обертка над нативным C11 автоматом Демпстера-Шефера (c_core/bio_dsp_dst.c)
с оптимальным транспортом Монжа-Канторовича W2 для адаптации англоязычной
модели Kintsugi DAM под русскоязычных пациентов и дисконтированием Шефера.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, asdict
from typing import Dict

from backend.bridges.bio_acoustic import AcousticBiomarkerVector
from backend.bridges.bio_native import (
    CAcousticMetrics,
    CCrossLingualCalib,
    CEvidentialResult,
    NativeBioMnetsLib,
)

_FOCAL_MASK_TO_LABEL: Dict[int, str] = {
    1: "Remission",
    2: "Relapse",
    4: "AdverseEffect",
    6: "Relapse_or_AdverseEffect",
    7: "Epistemic_Ignorance_Theta",
}

_SINGLETON_LABELS = ("Remission", "Relapse", "AdverseEffect")

_PHENOTYPE_CODE_TO_STR: Dict[int, str] = {
    0: "CONCORDANT_STABLE_TRAJECTORY",
    1: "ACTIVE_ANXIETY_DEPRESSIVE_RELAPSE",
    2: "PK_ADVERSE_EVENT_RISK",
    3: "SOMATOFORM_AGGRAVATION_SUSPECTED",
    4: "MASKED_RELAPSE_DISSIMULATION",
}

_TIER_CODE_TO_STR: Dict[int, str] = {
    1: "TIER_1_FAST_PATH_MONITORING",
    2: "TIER_2_CLINICAL_PSYCHOMETRIC_VERIFICATION",
    3: "TIER_3_MANDATORY_PSYCHIATRIST_ESCALATION",
}


@dataclass(frozen=True)
class EvidentialFusionReport:
    """Структурированный отчет C11-слияния Демпстера-Шефера и кросс-лингвистической адаптации."""
    bpa_ehr: Dict[str, float]
    bpa_dam_discounted: Dict[str, float]
    bpa_fused: Dict[str, float]
    belief: Dict[str, float]
    plausibility: Dict[str, float]
    pignistic_prob: Dict[str, float]
    dempster_conflict_k: float
    shafer_reliability_beta: float
    dissimulation_index: float
    aggravation_index: float
    epistemic_uncertainty: float
    dominant_hypothesis: str
    clinical_phenotype_flag: str
    recommended_routing_tier: str

    def to_dict(self) -> Dict:
        return asdict(self)


class DempsterShaferBioFusion:
    """
    Высокоскоростной C11-мост эвиденциального слияния с W2-транспортом EN -> RU.
    """

    def __init__(
        self,
        mu_en_dep: float = 0.32,
        sigma_en_dep: float = 0.14,
        mu_ru_dep: float = 0.48,
        sigma_ru_dep: float = 0.17,
        mu_en_anx: float = 0.30,
        sigma_en_anx: float = 0.14,
        mu_ru_anx: float = 0.44,
        sigma_ru_anx: float = 0.16,
        shafer_discount_gamma: float = 0.22,
    ) -> None:
        self._clib = NativeBioMnetsLib.get().lib
        self.calib = CCrossLingualCalib(
            mu_en_dep=mu_en_dep,
            sigma_en_dep=sigma_en_dep,
            mu_ru_dep=mu_ru_dep,
            sigma_ru_dep=sigma_ru_dep,
            mu_en_anx=mu_en_anx,
            sigma_en_anx=sigma_en_anx,
            mu_ru_anx=mu_ru_anx,
            sigma_ru_anx=sigma_ru_anx,
            shafer_discount_gamma=shafer_discount_gamma,
        )

    def evaluate_patient_multimodal(
        self,
        biomarkers: AcousticBiomarkerVector,
        hama_total: float,
        madrs_total: float,
        patient_self_report_wellbeing: float,
        raw_kintsugi_dep_score: float = -1.0,
        raw_kintsugi_anx_score: float = -1.0,
        adverse_event_markers_count: int = 0,
        cyp_inhibition_factor: float = 1.0,
        response_latency_sec: float = 1.8,
    ) -> EvidentialFusionReport:
        """Вызывает C11-ядро compass_dempster_shafer_bio_fuse."""
        c_metrics = CAcousticMetrics(
            f0_mean_hz=biomarkers.f0_mean_hz,
            f0_std_hz=biomarkers.f0_std_hz,
            jitter_ppq5_pct=biomarkers.jitter_ppq5_pct,
            shimmer_apq5_pct=biomarkers.shimmer_apq5_pct,
            glottal_tkeo_instability=biomarkers.glottal_tkeo_instability,
            glottal_tkeo_kurtosis=biomarkers.glottal_tkeo_kurtosis,
            weibull_pause_shape_k=biomarkers.weibull_pause_shape_k,
            weibull_pause_scale_lambda_ms=biomarkers.weibull_pause_scale_lambda_ms,
            cognitive_pause_fraction=biomarkers.cognitive_pause_fraction,
            ru_plosive_closure_fraction=biomarkers.ru_plosive_closure_fraction,
            lpc_spectral_tilt_db=biomarkers.lpc_spectral_tilt_db,
            hnr_db=biomarkers.hnr_db,
            snr_db=biomarkers.snr_db,
            ru_phonetic_shift_mahalanobis=biomarkers.ru_phonetic_shift_mahalanobis,
            z_score_depression_invariant=biomarkers.z_score_depression_invariant,
            z_score_anxiety_invariant=biomarkers.z_score_anxiety_invariant,
        )
        c_out = CEvidentialResult()

        rc = self._clib.compass_dempster_shafer_bio_fuse(
            ctypes.byref(c_metrics),
            ctypes.byref(self.calib),
            ctypes.c_double(raw_kintsugi_dep_score),
            ctypes.c_double(raw_kintsugi_anx_score),
            ctypes.c_double(hama_total),
            ctypes.c_double(madrs_total),
            ctypes.c_double(patient_self_report_wellbeing),
            ctypes.c_int32(adverse_event_markers_count),
            ctypes.c_double(cyp_inhibition_factor),
            ctypes.c_double(response_latency_sec),
            ctypes.byref(c_out),
        )
        if rc != 0:
            raise RuntimeError(f"compass_dempster_shafer_bio_fuse failed with code {rc}")

        bpa_ehr = {label: round(float(c_out.bpa_ehr[m]), 6) for m, label in _FOCAL_MASK_TO_LABEL.items()}
        bpa_dam = {label: round(float(c_out.bpa_dam_disc[m]), 6) for m, label in _FOCAL_MASK_TO_LABEL.items()}
        bpa_fused = {label: round(float(c_out.bpa_fused[m]), 6) for m, label in _FOCAL_MASK_TO_LABEL.items()}

        belief = {_SINGLETON_LABELS[i]: round(float(c_out.belief[i]), 6) for i in range(3)}
        plausibility = {_SINGLETON_LABELS[i]: round(float(c_out.plausibility[i]), 6) for i in range(3)}
        pignistic = {_SINGLETON_LABELS[i]: round(float(c_out.pignistic_prob[i]), 6) for i in range(3)}

        dom_idx = int(c_out.dominant_singleton_idx)
        pheno_code = int(c_out.phenotype_flag_code)
        tier_code = int(c_out.recommended_tier)

        return EvidentialFusionReport(
            bpa_ehr=bpa_ehr,
            bpa_dam_discounted=bpa_dam,
            bpa_fused=bpa_fused,
            belief=belief,
            plausibility=plausibility,
            pignistic_prob=pignistic,
            dempster_conflict_k=round(float(c_out.dempster_conflict_k), 6),
            shafer_reliability_beta=round(float(c_out.shafer_reliability_beta), 6),
            dissimulation_index=round(float(c_out.dissimulation_index), 6),
            aggravation_index=round(float(c_out.aggravation_index), 6),
            epistemic_uncertainty=round(float(c_out.epistemic_uncertainty), 6),
            dominant_hypothesis=_SINGLETON_LABELS[dom_idx],
            clinical_phenotype_flag=_PHENOTYPE_CODE_TO_STR.get(pheno_code, "CONCORDANT_STABLE_TRAJECTORY"),
            recommended_routing_tier=_TIER_CODE_TO_STR.get(tier_code, "TIER_1_FAST_PATH_MONITORING"),
        )