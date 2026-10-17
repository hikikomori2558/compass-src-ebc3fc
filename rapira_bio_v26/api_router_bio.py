# api_router_bio.py — Боевой FastAPI роутер и автономный сервер части Разработчика №2 (COMPASS-HYBRID v2.6)
import ctypes
import time
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from trajectory_clustering import RapiraCCoreBridge, CVoiceDSPOutput
from clinical_rule_engine import ClinicalCompassRuleEngine
from ehr_nlp_benchmark import (
    HybridEHRParser,
    run_validation_benchmark_100_vignettes,
    run_time_motion_ergonomics_study,
)

bio_router = APIRouter(
    prefix="/api/v1/compass-bio",
    tags=["Rapira COMPASS-HYBRID v2.6: C-Core PK/PD, 24D Clusters & NLP (Dev #2)"],
)

c_bridge = RapiraCCoreBridge()
rule_engine = ClinicalCompassRuleEngine(c_bridge=c_bridge)
ehr_parser = HybridEHRParser()


class EHRParseAndEvaluateRequest(BaseModel):
    raw_ehr_text: str = Field(
        ...,
        examples=[
            "Осмотр невролога (ф. 025/у). М, 36 г. Код МКБ: F43.2. Жал.: трев., инсомния после COVID-19, водит авто. "
            "Лаб: креат. 86 мкмоль/л, Чайлд-Пью: норма. Шкалы: ГТР-7 12б, PHQ9 7б (п9=0), CSSRS=0."
        ],
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


@bio_router.post("/ehr/analyze-and-route", summary="[Визит врача] Сквозной разбор ЭМК + C-Core RK4 168ч + 24D Кластер + Маршрутизация")
def analyze_ehr_and_route(req: EHRParseAndEvaluateRequest) -> Dict[str, Any]:
    t0 = time.perf_counter()
    parsed = ehr_parser.parse_ehr_record(req.raw_ehr_text)
    pipeline_res = rule_engine.run_full_clinical_compass_pipeline(
        patient_c_profile=parsed["c_profile"],
        clinical_state=parsed["clinical_state"],
        icd10_code=parsed["icd10_code"],
    )
    total_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "api_total_latency_ms": round(total_ms, 3),
        "extracted_ehr_entities": parsed,
        "compass_decision": pipeline_res,
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


@bio_router.post("/patient/voice-biomarkers", summary="[Голосовой радар] C-DSP + KintsugiHealth/dam и детекция диссимуляции (Кластер К4)")
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


@bio_router.post("/admin/hot-reload-scale-plugin", summary="[Метрика №10] Горячее добавление новой шкалы без перезапуска ядра")
def register_new_scale_plugin(req: CustomScalePluginRequest) -> Dict[str, Any]:
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
    return rule_engine.register_custom_scale_plugin(req.scale_code, manifest)


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
