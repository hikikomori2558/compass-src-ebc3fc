# -*- coding: utf-8 -*-
"""
backend/api/cdss_workstation.py
===============================
Промышленный REST API роутер для АРМ Врача (frontend_doctor/),
связывающий интерфейс напрямую с нативными C11-движками:
- C11PopPKAndBitapEngine (RK45 168ч фармакокинетика Авиандра + 64-bit Bitap NER)
- HybridBioControlBridge (LPC-4/TKEO акустика + Демпстер-Шефер + W8A8 Микросети)
- C11TrajectoryOTAndXAIOrchestrator (Синкхорн-Вассерштейн 36 нед. + Оуэн-Шепли)
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.api.auth import get_principal
from backend.bridges.lexers import C11PopPKAndBitapEngine
from backend.domain.phenotypes import detect_phenotypes
from backend.paths import KB_PATH
from backend.bridges.ot_xai import C11TrajectoryOTAndXAIOrchestrator, DEFAULT_HEALTHY_BASELINE_24D
from backend.services.bio_control import HybridBioControlBridge

router = APIRouter(
    prefix="/api/v1/cdss",
    tags=["CDSS Physician Workstation"],
    # Единая политика безопасности: клинические эндпоинты не бывают анонимными.
    dependencies=[Depends(get_principal)],
)

_kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
_pk_lexer_engine = C11PopPKAndBitapEngine()
_ot_xai_orchestrator = C11TrajectoryOTAndXAIOrchestrator()
_bio_control_bridge = HybridBioControlBridge()


class MedicationOrderItem(BaseModel):
    drug_name: str
    dose_mg: float = 20.0
    interval_hours: float = 12.0
    active: bool = True


class EncounterEvaluationRequest(BaseModel):
    patient_id: str = "RU-77-4092"
    doctor_specialty: str = Field(default="therapist", description="therapist | neurologist | psychiatrist")
    ehr_note_text: str
    hama_score: float = 6.0
    madrs_score: float = 7.0
    phq9_score: float = 4.0
    gad7_score: float = 3.0
    phq9_item9_suicidal: int = 0
    self_report_wellbeing: float = 0.92
    cyp2d6_activity_score: float = 0.12
    hepatic_child_pugh_factor: float = 1.0
    egfr_ml_min: float = 78.0
    aviandr_dose_mg: float = 20.0
    aviandr_interval_h: float = 12.0
    active_comedications: List[str] = Field(default_factory=list)
    telemetry_summary: Optional[Dict[str, float]] = None
    raw_kintsugi_dep_score: float = 0.84
    raw_kintsugi_anx_score: float = 0.87
    response_latency_sec: float = 4.2


@router.post("/evaluate-encounter")
def evaluate_clinical_encounter(req: EncounterEvaluationRequest) -> Dict[str, Any]:
    """
    Выполняет полный цикл расчета в C11 при любом изменении текста осмотра,
    списка препаратов (CPOE) или баллов психометрических шкал.
    """
    t0 = time.perf_counter()

    # 1. C11 Bitap NER по тексту истории болезни
    entities = _pk_lexer_engine.scan_ehr_text(req.ehr_note_text)
    active_entities = [e for e in entities if not e.is_negated]

    # Суммируем ингибиторы CYP2D6 и CYP3A4 из текста ЭМК и активного листа назначений CPOE
    comed_text = " ".join(req.active_comedications)
    comed_entities = _pk_lexer_engine.scan_ehr_text(comed_text) if comed_text else []
    combined_active = active_entities + [e for e in comed_entities if not e.is_negated]

    seen_rules = set()
    dedup_active = []
    for e in combined_active:
        if e.rule_id not in seen_rules:
            seen_rules.add(e.rule_id)
            dedup_active.append(e)

    cyp2d6_inh_sum = sum(
        e.ddi_ki_ratio_impact for e in dedup_active if e.category_code == 3 and e.ddi_ki_ratio_impact > 0
    )
    cyp3a4_inh_sum = sum(
        e.ddi_ki_ratio_impact for e in dedup_active if e.category_code == 4 and e.ddi_ki_ratio_impact > 0
    )
    mbi_2d6 = 0.35 if any(e.rule_id == 302 for e in dedup_active) else 1.0
    hard_stops = [e.canonical_name for e in dedup_active if e.category_code == 2]

    # 2. C11 Dormand-Prince RK45 168h симуляция для выбранного режима Авиандра
    pk_sol = _pk_lexer_engine.simulate_poppk_rk45(
        dose_mg=req.aviandr_dose_mg,
        dosing_interval_hours=req.aviandr_interval_h,
        total_doses_count=int(168.0 // req.aviandr_interval_h),
        cyp2d6_activity_score=req.cyp2d6_activity_score,
        cyp2d6_inhibitor_ratio_sum=cyp2d6_inh_sum,
        cyp2d6_mbi_remaining_frac=mbi_2d6,
        cyp3a4_inhibitor_ratio_sum=cyp3a4_inh_sum,
        hepatic_child_pugh_factor=req.hepatic_child_pugh_factor,
        egfr_ml_min=req.egfr_ml_min,
    )

    # 3. C11 Био-контур (LPC-4/TKEO + Демпстер-Шефер) и 3 микросети W8A8
    bio_ctrl = _bio_control_bridge.process_clinical_encounter(
        hama_total=req.hama_score,
        madrs_total=req.madrs_score,
        patient_self_report_wellbeing=req.self_report_wellbeing,
        cyp2d6_activity_score=req.cyp2d6_activity_score,
        cyp3a4_inhibition_factor=1.0 + 0.35 * cyp3a4_inh_sum,
        egfr_norm=req.egfr_ml_min / 90.0,
        hard_stop_triggered=(len(hard_stops) > 0 or pk_sol.c_max_ng_ml >= 320.0),
        telemetry_summary=req.telemetry_summary,
        raw_kintsugi_dep_score=req.raw_kintsugi_dep_score,
        raw_kintsugi_anx_score=req.raw_kintsugi_anx_score,
        response_latency_sec=req.response_latency_sec,
    )

    # 4. C11 Точные коалиционные значения Оуэна (4096 коалиций)
    dst_rep = bio_ctrl["evidential_dempster_shafer"]
    bio_vec = bio_ctrl["acoustic_biomarkers"]
    pat_24d = DEFAULT_HEALTHY_BASELINE_24D.copy()
    pat_24d[0] = min(1.8, req.hama_score / 30.0)
    pat_24d[1] = min(1.8, req.madrs_score / 34.0)
    pat_24d[3] = req.self_report_wellbeing
    pat_24d[4] = req.cyp2d6_activity_score
    pat_24d[5] = 1.0 + 0.35 * (cyp2d6_inh_sum + cyp3a4_inh_sum)
    pat_24d[6] = req.egfr_ml_min / 90.0
    pat_24d[8] = 0.65 if pk_sol.c_max_ng_ml >= 320.0 else 0.12
    pat_24d[9] = 1.0 if len(hard_stops) > 0 else 0.0
    pat_24d[12] = bio_vec["glottal_tkeo_instability"]
    pat_24d[15] = bio_vec["weibull_pause_shape_k"]
    pat_24d[18] = dst_rep["dempster_conflict_k"]
    pat_24d[19] = dst_rep["dissimulation_index"]
    pat_24d[23] = 1.0 if (len(hard_stops) > 0 or pk_sol.c_max_ng_ml >= 320.0) else 0.0

    owen_xai = _ot_xai_orchestrator.explain_with_exact_owen_and_counterfactual(pat_24d)

    # 5. Вычисление категории направления к психиатру ("ОБЯЗАТЕЛЬНО" / "ЖЕЛАТЕЛЬНО" / "ПЛАНОВО")
    is_mandatory_psych = (
        req.phq9_item9_suicidal >= 1
        or len(hard_stops) > 0
        or dst_rep["dissimulation_index"] >= 0.35
        or req.madrs_score >= 30.0
        or pk_sol.c_max_ng_ml >= 320.0
    )
    is_recommended_psych = (
        not is_mandatory_psych
        and (req.hama_score >= 18.0 or req.madrs_score >= 20.0 or req.gad7_score >= 12.0)
    )

    if is_mandatory_psych:
        routing_category = "ОБЯЗАТЕЛЬНО"
        routing_indicator = "critical"
    elif is_recommended_psych:
        routing_category = "ЖЕЛАТЕЛЬНО"
        routing_indicator = "warning"
    else:
        routing_category = "ПЛАНОВОЕ НАБЛЮДЕНИЕ"
        routing_indicator = "info"

    total_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "latency_ms": round(total_ms, 3),
        "routing_category": routing_category,
        "routing_indicator": routing_indicator,
        "extracted_entities": [e.to_dict() for e in entities],
        "active_contraindications": hard_stops,
        "poppk_rk45": pk_sol.to_dict(),
        "bio_and_micronets": bio_ctrl,
        "owen_xai": owen_xai.to_dict(),
        # Карточки клинических фенотипов (6 целевых профилей): подсказки врачу,
        # детекция детерминированная по тексту ЭМК (backend/domain/phenotypes.py).
        "phenotype_guidance": detect_phenotypes(req.ehr_note_text, {}, _kb),
    }