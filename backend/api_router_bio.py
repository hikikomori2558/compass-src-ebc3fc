# api_router_bio.py — Боевой FastAPI роутер и автономный сервер части Разработчика №2 (COMPASS-HYBRID v2.6)
import ctypes
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, Field

# Плоские импорты биоконтура (trajectory_clustering / clinical_rule_engine / ehr_nlp_benchmark)
# работают и как `python3 backend/api_router_bio.py`, и как `uvicorn backend.api_router_bio:app`
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from trajectory_clustering import RapiraCCoreBridge, CVoiceDSPOutput  # noqa: E402
from clinical_rule_engine import ClinicalCompassRuleEngine  # noqa: E402
from safe_pipeline import SafeCompassPipeline  # noqa: E402
from ehr_nlp_benchmark import (  # noqa: E402
    run_validation_benchmark_100_vignettes,
    run_time_motion_ergonomics_study,
)
from api_auth import get_principal, require_roles  # noqa: E402
from auth import Principal  # noqa: E402
from evidence_xai_cbr import EvidenceAndXAIEngine  # noqa: E402  (24e23de: SHAP ГОСТ Р 59921 + CBR)

bio_router = APIRouter(
    prefix="/api/v1/compass-bio",
    tags=["Rapira COMPASS-HYBRID v2.6: C-Core PK/PD, 24D Clusters & NLP (Dev #2)"],
    # Безопасность (долг репо, 152-ФЗ): клинические эндпоинты не бывают анонимными —
    # любой вызов требует валидного Bearer (врач/пациент/админ). Горячая загрузка
    # шкал — дополнительно только методисту (см. ниже).
    dependencies=[Depends(get_principal)],
)

c_bridge = RapiraCCoreBridge()
rule_engine = ClinicalCompassRuleEngine(c_bridge=c_bridge)
# Единственный путь разбора ЭМК для врача: tri-state, отрицания, давность анализов
# (контракт — docs/SAFE_PARSER.md). Старый HybridEHRParser остаётся только в
# benchmarks/ как регрессионный эталон и через API не доступен.
safe_pipeline = SafeCompassPipeline(rule_engine)
# Объяснимость: SHAP-атрибуции 24D по ГОСТ Р 59921 + Case-Based Reasoning по 5 РКИ.
xai_cbr_engine = EvidenceAndXAIEngine()


class EHRParseAndEvaluateRequest(BaseModel):
    raw_ehr_text: str = Field(
        ...,
        examples=[
            "Осмотр невролога (ф. 025/у). М, 36 г. Код МКБ: F43.2. Жал.: трев., инсомния после COVID-19, водит авто. "
            "Лаб: креат. 86 мкмоль/л, Чайлд-Пью: норма. Шкалы: ГТР-7 12б, PHQ9 7б (п9=0), CSSRS=0."
        ],
    )
    doctor_confirmations: Dict[str, bool] = Field(
        default_factory=dict,
        description="Подтверждения врача для пунктов, которых нет в ЭМК, например "
                    "{'no_epilepsy_or_organic_cns': true}. Список недостающих приходит в "
                    "doctor_confirmations_needed. Подтверждение не затирает найденное в ЭМК.",
    )


class PatientFollowupCheckinRequest(BaseModel):
    indication: str = Field(default="GAD")
    week_of_therapy: int = Field(default=1, ge=0, le=52)
    current_score: float = Field(default=12.0)
    baseline_score: float = Field(default=18.0)
    doses_taken: int = Field(default=13, ge=0)
    doses_scheduled: int = Field(default=14, ge=1)
    kop25_score: int = Field(default=78, ge=0, le=100)
    reported_mild_drowsiness_day_1_5: bool = Field(default=True)
    missed_last_dose: bool = Field(default=False)


class VoiceBiomarkerCheckinRequest(BaseModel):
    pcm_samples_16khz: Optional[List[float]] = Field(default=None)
    precomputed_acoustics: Optional[Dict[str, float]] = Field(default=None)
    self_reported_phq9: int = Field(default=6, ge=0, le=27)
    self_reported_gad7: int = Field(default=7, ge=0, le=21)


class CustomScalePluginRequest(BaseModel):
    scale_code: str = Field(default="ISI-7")
    title_ru: str = Field(default="Индекс тяжести инсомнии (ISI-7)")
    target_role: str = Field(default="PATIENT_SELF_REPORT")
    items_count: int = Field(default=7, ge=1)
    authors_ru: str = Field(default="Рассказова Е.И., Тхостов А.Ш.")
    year: int = Field(default=2024)
    citation: str = Field(default="Валидация русскоязычной версии ISI")
    license_type: str = Field(default="Validated Academic/Clinical Use")
    cutoffs: Dict[str, Dict[str, str]] = Field(
        default={
            "0_7": {"level": "NORMAL", "label_ru": "Норма"},
            "8_14": {"level": "SUBTHRESHOLD", "label_ru": "Подпороговая бессонница"},
            "15_28": {"level": "CLINICAL_INSOMNIA", "label_ru": "Клиническая бессонница"},
        }
    )


@bio_router.post("/ehr/analyze-and-route", summary="[Визит врача] Безопасный tri-state разбор ЭМК -> правила ОХЛП -> C-Core RK4 168ч -> стратификация -> ГОСТ SHAP XAI + CBR")
def analyze_ehr_and_route(req: EHRParseAndEvaluateRequest) -> Dict[str, Any]:
    t0 = time.perf_counter()
    res = safe_pipeline.analyze_text(req.raw_ehr_text, confirmations=req.doctor_confirmations)
    ex = res["extracted"]
    cluster = res["cluster_24d_analysis"]
    m1_ctrough = res["c_core_pk_and_safety"]["pk_summary_168h"]["m1_ctrough_ss_pg_ml"]
    is_blocked = res["rules_engine"]["status"] == "RED_HARD_STOP"

    # XAI (SHAP по ГОСТ Р 59921) и CBR опираются на 24D-вектор: без PHQ-9/GAD-7
    # объяснять нечего — оба поля возвращаются None, и это честный null, а не ноль.
    shap_xai = similar_rki_cohorts = None
    if "state_vector_24d" in cluster:
        shap_xai = xai_cbr_engine.compute_shap_attributions_24d(
            cluster["state_vector_24d"], assigned_cluster=cluster["assigned_cluster_code"])
        cs = ex["clinical_state"]
        similar_rki_cohorts = xai_cbr_engine.find_similar_rki_cohorts_cbr(
            icd10_code=ex["icd10_code"] or "",
            gad7=float(cs["gad7_total"]), phq9=float(cs["phq9_total"]),
            moca=27.4,  # MoCA не вводится (лицензия): значение условное, см. SAFE_PARSER.md
            m1_ctrough=float(m1_ctrough),
        )
    provenance_passport = xai_cbr_engine.build_authority_provenance_passport(
        icd10_code=ex["icd10_code"] or "", is_blocked=is_blocked,
    )

    total_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "api_total_latency_ms": round(total_ms, 3),
        "overall_status": res["overall_status"],
        "data_required": res["data_required"],
        "doctor_confirmations_needed": res["doctor_confirmations_needed"],
        "extracted_ehr_entities": ex,
        "compass_decision": res,
        "xai_gost_59921_shap": shap_xai,
        "case_based_reasoning_rki_cohorts": similar_rki_cohorts,
        "evidence_provenance_passport": provenance_passport,
    }


@bio_router.post("/patient/followup-checkin", summary="[Дневник пациента] Динамика терапии, удержание К2 и правило пропуска дозы")
def evaluate_patient_checkin(req: PatientFollowupCheckinRequest) -> Dict[str, Any]:
    return rule_engine.evaluate_patient_followup_and_adherence(
        indication=req.indication,
        week=req.week_of_therapy,
        current_score=req.current_score,
        baseline_score=req.baseline_score,
        doses_taken=req.doses_taken,
        doses_scheduled=req.doses_scheduled,
        kop25_score=req.kop25_score,
        reported_mild_drowsiness_day_1_5=req.reported_mild_drowsiness_day_1_5,
        missed_last_dose=req.missed_last_dose,
    )


@bio_router.post("/patient/voice-biomarkers", summary="[Голосовой радар, ИССЛЕДОВАТЕЛЬСКИЙ РЕЖИМ] C-DSP-фичи; классы DAM — вне клинических решений (П1)")
def analyze_voice_biomarkers(req: VoiceBiomarkerCheckinRequest) -> Dict[str, Any]:
    t0 = time.perf_counter()
    if req.pcm_samples_16khz and len(req.pcm_samples_16khz) > 0:
        n = len(req.pcm_samples_16khz)
        c_arr = (ctypes.c_float * n)(*req.pcm_samples_16khz)
        c_out = CVoiceDSPOutput()
        c_bridge.lib.extract_voice_dsp_v26(c_arr, n, ctypes.byref(c_out))
        acoustics = {
            "f0_mean_hz": round(float(c_out.f0_mean_hz), 2),
            "f0_std_hz": round(float(c_out.f0_std_hz), 2),
            "jitter_local_pct": round(float(c_out.jitter_local_pct), 3),
            "shimmer_local_pct": round(float(c_out.shimmer_local_pct), 3),
            "hnr_db": round(float(c_out.hnr_db), 2),
            "pause_ratio": round(float(c_out.pause_ratio), 4),
            "speech_rate_syl_sec": round(float(c_out.speech_rate_syl_sec), 2),
            "rms_energy": round(float(c_out.rms_energy), 4),
        }
    elif req.precomputed_acoustics:
        acoustics = req.precomputed_acoustics
    else:
        acoustics = {"pause_ratio": 0.45, "jitter_local_pct": 1.7, "shimmer_local_pct": 6.4, "rms_energy": 0.04}

    res = rule_engine.voice_analyzer.analyze_voice_checkin(
        acoustic_features=acoustics,
        self_reported_phq9=req.self_reported_phq9,
        self_reported_gad7=req.self_reported_gad7,
    )
    res["compute_latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    return res


@bio_router.get("/scales-and-trials-registry", summary="[Справочник ГРЛС и Шкалы] Паспорта валидации, лицензии и коридор РКИ Волель 2025")
def get_scales_and_trials_registry() -> Dict[str, Any]:
    return {
        "scales_registry": rule_engine.kb["psychometric_scales_registry"],
        "aviandr_reference_corridors": rule_engine.kb["aviandr_clinical_trials_reference_corridors"],
        "therapy_grls_catalog": rule_engine.kb["therapy_options_grls_catalog"],
    }


@bio_router.post("/admin/hot-reload-scale-plugin", summary="[Метрика №10] Горячее добавление новой шкалы (только ADMIN_METHODOLOGIST)")
def register_new_scale_plugin(req: CustomScalePluginRequest,
                              principal: Principal = Depends(require_roles("ADMIN_METHODOLOGIST"))) -> Dict[str, Any]:
    # Метрика №10 без дыры: менять клинический реестр имеет право только врач-методист,
    # и его личность фиксируется в ответе (в боевой версии — ещё и в audit_log).
    manifest = {
        "code": req.scale_code,
        "title_ru": req.title_ru,
        "target_role": req.target_role,
        "items_count": req.items_count,
        "validation_passport": {
            "authors_ru": req.authors_ru,
            "year": req.year,
            "citation": req.citation,
            "license_type": req.license_type,
            "recommended_by_minzdrav_kr": True,
        },
        "cutoffs": req.cutoffs,
    }
    result = rule_engine.register_custom_scale_plugin(req.scale_code, manifest)
    result["registered_by"] = principal.sub
    result["registered_by_role"] = principal.role
    return result


@bio_router.get("/telemetry/full-benchmark-report", summary="[Телеметрия Жюри] Прогон 100 синтетических ЭМК (Метрики №1, 2, 3, 4)")
def get_live_benchmark_telemetry() -> Dict[str, Any]:
    return {
        "nlp_and_safety_benchmark_100_cases": run_validation_benchmark_100_vignettes(),
        "ergonomics_time_motion_study": run_time_motion_ergonomics_study(),
    }


app = FastAPI(title="Rapira COMPASS-HYBRID v2.6 — Bioinformatics & Clinical Core API")
app.include_router(bio_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
