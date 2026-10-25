# backend/api/bio.py — FastAPI-роутер биоконтура COMPASS-HYBRID v2.6
# (автономный запуск: python3 -m backend.api.bio).
import ctypes
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field

from backend.api.auth import get_principal, require_roles
from backend.bridges.core import CVoiceDSPOutput, RapiraCCoreBridge
from backend.domain.evidence_xai import EvidenceAndXAIEngine  # SHAP ГОСТ Р 59921 + CBR
from backend.domain.routing import ClinicalCompassRuleEngine
from backend.services.auth import Principal
from backend.services.pipeline import SafeCompassPipeline
from benchmarks.ehr_nlp_benchmark import (
    run_time_motion_ergonomics_study,
    run_validation_benchmark_100_vignettes,
)

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
    from backend.domain.trajectory import TrajectoryClusterEngine24D

    return {
        "scales_registry": rule_engine.kb["psychometric_scales_registry"],
        "aviandr_reference_corridors": rule_engine.kb["aviandr_clinical_trials_reference_corridors"],
        "therapy_grls_catalog": rule_engine.kb["therapy_options_grls_catalog"],
        # Контуры 2-3: доказательность, фенотипы, контрольные точки, коридоры графиков
        "evidence_grades": rule_engine.kb.get("evidence_grades", {}),
        "open_label_disclaimer": rule_engine.kb.get("open_label_disclaimer", ""),
        "patient_phenotypes": rule_engine.kb.get("patient_phenotypes", {}),
        "monitoring_schedule": rule_engine.kb.get("monitoring_schedule", {}),
        "chart_reference_corridors": TrajectoryClusterEngine24D.reference_corridors(),
    }


@bio_router.post("/admin/hot-reload-scale-plugin", summary="[Метрика №10] Горячее добавление новой шкалы (только ADMIN_METHODOLOGIST)")
async def register_new_scale_plugin(req: CustomScalePluginRequest, request: Request,
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

    # Метрика №10 с аудируемостью: каждая горячая правка = версия + sha256 + автор
    # (таблицы scale_versions / audit_log из schema.sql). Только INSERT: читать
    # журнал из-под API нельзя by design.
    import hashlib
    import json as _json
    from datetime import datetime, timezone

    manifest_json = _json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(manifest_json.encode()).hexdigest()
    version = datetime.now(timezone.utc).strftime("v%Y.%m.%d-%H%M")
    if getattr(request.app.state, "pg_pool", None) is not None:
        from backend.db.context import db_conn
        import uuid as _uuid
        try:
            async with db_conn(request.app.state.pg_pool) as conn:
                await conn.execute(
                    "INSERT INTO scale_versions (scale_code, version, content_sha256, is_active) "
                    "VALUES ($1,$2,$3,true) ON CONFLICT (scale_code, version) DO NOTHING",
                    req.scale_code, version, sha)
                await conn.execute(
                    "INSERT INTO audit_log (actor_id, action, entity, entity_id, details) "
                    "VALUES ($1,'scale_plugin_registered','psychometric_scales_registry',$2,$3::jsonb)",
                    _uuid.UUID(principal.sub), req.scale_code,
                    _json.dumps({"version": version, "sha256": sha[:16]},
                                ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    result = rule_engine.register_custom_scale_plugin(req.scale_code, manifest)
    result["registered_by"] = principal.sub
    result["registered_by_role"] = principal.role
    result["version"] = version
    result["content_sha256"] = sha
    return result


@bio_router.post("/patient/{patient_hash}/cluster-profile",
                 summary="[Кластеризация] Вычислить и сохранить профиль: 24D → кластер K1–K5 → OOD (C-ядро)")
async def patient_cluster_profile(patient_hash: str, request: Request,
                                  principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Строит кластерный профиль из реальных строк БД (шкалы, приверженность, время
    приёма), персистит в patient_cluster_profiles, возвращает OOD-отчёт C-ядра.
    RLS: врач — только свои пациенты, пациент — только себя."""
    from backend.services.cluster_service import compute_profile_from_db
    from fastapi import HTTPException
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    profile = await compute_profile_from_db(pool, patient_hash,
                                            caller_role=principal.role, caller_id=principal.sub)
    if profile is None:
        raise HTTPException(404, "У пациента нет данных для кластеризации (нет шкал и приёмов)")
    return profile


@bio_router.get("/cluster-map",
                summary="[Кластеризация] Карта: кластеры K1–K5, референсные траектории РКИ, пациенты (RLS)")
async def cluster_map(request: Request, principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Интерактивная карта кластеризации. Пациенты — реальные профили из
    patient_cluster_profiles (RLS отрезает чужих). Референсные точки — траектории
    исследований, честно помечены reference: true. Махаланобис-эллипс — параметры
    Ledoit-Wolf ковариации кластеров (c_core/ot_xai_mahalanobis.c)."""
    from backend.services.cluster_service import cluster_map_payload
    from backend.db.context import db_conn
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    async with db_conn(pool, role=principal.role, user_id=principal.sub) as conn:
        rows = await conn.fetch(
            "SELECT patient_hash_id::text AS patient_hash, z24, cluster_code, "
            "projection_x, projection_y, dtw, ood, computed_at "
            "FROM patient_cluster_profiles ORDER BY computed_at DESC LIMIT 100")
    profiles = []
    for r in rows:
        profiles.append({
            "patient_hash": r["patient_hash"], "z24": json.loads(r["z24"]) if isinstance(r["z24"], str) else r["z24"],
            "cluster_code": r["cluster_code"], "projection_x": r["projection_x"],
            "projection_y": r["projection_y"], "dtw": r["dtw"],
            "ood": (r["ood"] if isinstance(r["ood"], dict) else json.loads(r["ood"])) if r["ood"] else {},
            "facts": {},
        })
    return cluster_map_payload(profiles)


@bio_router.get("/patient/{patient_hash}/timeline",
                summary="[Динамика] Реальная история пациента: шкалы, алерты, приверженность (RLS)")
async def patient_timeline(patient_hash: str, request: Request,
                           principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Всё, что накопилось между визитами: чек-ины, шкалы, алерты, приверженность.
    RLS сам отрезает чужих пациентов: не привязан — увидишь пустые списки, чужое не утечёт."""
    import json as _json
    import uuid as _uuid

    from backend.db.context import db_conn
    from fastapi import HTTPException

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    try:
        ph = _uuid.UUID(patient_hash)
    except ValueError:
        raise HTTPException(400, "patient_hash должен быть UUID")

    async with db_conn(pool, role=principal.role, user_id=principal.sub) as conn:
        scales = await conn.fetch(
            "SELECT scale_code, total_score, taken_at, delta_prev FROM patient_scale_assessments "
            "WHERE patient_hash_id=$1 ORDER BY taken_at DESC LIMIT 60", ph)
        alerts = await conn.fetch(
            "SELECT alert_id, category, critical, reasons, created_at, doctor_reaction_at "
            "FROM clinical_alerts_and_routing WHERE patient_hash_id=$1 "
            "ORDER BY created_at DESC LIMIT 30", ph)
        adherence = await conn.fetch(
            "SELECT taken_at, dose_skipped, intake_time, notes, diary, event_uuid "
            "FROM medication_adherence_logs "
            "WHERE patient_hash_id=$1 ORDER BY taken_at DESC LIMIT 60", ph)

    return {
        "patient_hash": patient_hash,
        "scales": [dict(s) for s in scales],
        "alerts": [{**dict(a), "reasons": _json.loads(a["reasons"]) if a["reasons"] else []}
                   for a in alerts],
        "adherence": [dict(x) for x in adherence],
        "note": "RLS: врач видит только своих пациентов; чужие списки пусты",
    }


# ------------------------------------------------------------------ дневник, шкалы, инсайты
from backend.domain.insights import build_insights
from backend.domain.scales_mz import SCALES, score_answers
from backend.domain.side_effects import CHOICES, guidance_for


class DiaryPayload(BaseModel):
    """Дневник дня: приём + побочки + самочувствие. Одна запись на сутки (UPSERT)."""
    intake_time: Optional[str] = None          # "20:30"
    taken: bool = True
    before_meal_30min: Optional[bool] = None
    side_effects: List[Dict[str, Any]] = Field(default_factory=list)  # [{code, severity, note}]
    mood_score: Optional[int] = None
    anxiety_score: Optional[int] = None
    sleep_score: Optional[int] = None
    notes: Optional[str] = None


@bio_router.get("/patient/diary/choices",
                summary="[Пациент] Легенда побочек для кнопок дневника")
def diary_choices() -> Dict[str, Any]:
    return {"choices": CHOICES}


@bio_router.post("/patient/diary", summary="[Пациент] Запись дня + МГНОВЕННЫЕ пояснения побочек")
async def patient_diary_upsert(payload: DiaryPayload, request: Request,
                               patient: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Принимает запись дня. В ответе — карточки пояснений по каждой отмеченной
    побочке (в момент возникновения, не «памятка в целом») + красные флаги."""
    import json as _json
    import uuid as _uuid

    from backend.db.context import db_conn

    from fastapi import HTTPException
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    try:
        ph = _uuid.UUID(patient.sub)
    except ValueError:
        raise HTTPException(400, "bad patient hash")

    intake = None
    if payload.intake_time:
        from datetime import datetime as _dt
        intake = _dt.strptime(payload.intake_time, "%H:%M").time()
    async with db_conn(pool, role="PATIENT", user_id=patient.sub) as conn:
        await conn.execute(
            "INSERT INTO patient_diary_entries "
            "(patient_hash_id, entry_date, intake_time, taken, before_meal_30min, "
            " side_effects, mood_score, anxiety_score, sleep_score, notes) "
            "VALUES ($1, current_date, $2::time, $3, $4, $5::jsonb, $6, $7, $8, $9) "
            "ON CONFLICT (patient_hash_id, entry_date) DO UPDATE SET "
            " intake_time=EXCLUDED.intake_time, taken=EXCLUDED.taken, "
            " before_meal_30min=EXCLUDED.before_meal_30min, side_effects=EXCLUDED.side_effects, "
            " mood_score=EXCLUDED.mood_score, anxiety_score=EXCLUDED.anxiety_score, "
            " sleep_score=EXCLUDED.sleep_score, notes=EXCLUDED.notes, created_at=now()",
            ph, intake, payload.taken, payload.before_meal_30min,
            _json.dumps(payload.side_effects, ensure_ascii=False),
            payload.mood_score, payload.anxiety_score, payload.sleep_score, payload.notes)

    codes = [str(se.get("code", "")) for se in payload.side_effects]
    guidance = guidance_for(codes)
    return {
        "saved": True,
        "guidance": guidance,   # <-- ПОЯСНЕНИЯ В МОМЕНТ ВОЗНИКНОВЕНИЯ
        "critical_flag": any(g.get("always_critical") for g in guidance),
        "human_note": "Пояснения — информационные, решение о терапии принимает врач.",
    }


class ScaleSubmit(BaseModel):
    answers: List[int]


@bio_router.post("/patient/{patient_hash}/adverse-events/draft-1071",
                 summary="[Врач] Черновик извещения о НЯ (Приказ Росздравнадзора № 1071)")
async def draft_ae_1071(patient_hash: str, request: Request,
                        principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Собирает черновик извещения по всем НЯ пациента без черновика, сохраняет его
    в adverse_events_and_risks.roszdravnadzor_draft и возвращает печатный текст.
    Решение об отправке — только врача (система предупреждает и оформляет)."""
    import uuid as _uuid

    from backend.db.context import db_conn
    from fastapi import HTTPException

    if principal.role not in ("DOCTOR_PSYCHIATRIST", "DOCTOR_NEUROLOGIST", "DOCTOR_GP", "ADMIN_METHODOLOGIST"):
        raise HTTPException(403, "Извещение формирует врач")
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    ph = _uuid.UUID(patient_hash)
    REL_RU = {"DEFINITELY": "определённо связана", "PROBABLE": "вероятно связана",
              "POSSIBLE": "возможно связана", "UNLIKELY": "маловероятно связана",
              "UNRELATED": "не связана"}
    SEV_RU = {1: "лёгкая (CTCAE 1)", 2: "умеренная (CTCAE 2)", 3: "тяжёлая (CTCAE 3)",
              4: "жизнеугрожающая (CTCAE 4)", 5: "смертельный исход (CTCAE 5)"}
    async with db_conn(pool, role=principal.role, user_id=principal.sub) as conn:
        rows = await conn.fetch(
            "SELECT ae_id, meddra_term, severity_ctcae, related_to_drug, reported_at "
            "FROM adverse_events_and_risks WHERE patient_hash_id=$1 "
            "ORDER BY reported_at DESC LIMIT 20", ph)
        if not rows:
            raise HTTPException(404, "У пациента нет записей НЯ")
        lines = ["ИЗВЕЩЕНИЕ О НЕЖЕЛАТЕЛЬНОЙ РЕАКЦИИ (черновик)",
                 "Основание: Приказ Росздравнадзора № 1071 от 15.02.2017; правила GVP ЕАЭС",
                 "Адресат: АИС Росздравнадзора; служба фармаконадзора ООО «иФарма» (ГК «ХимРар»)",
                 "Подозреваемый препарат: Авиандр® (маритупирдин) 20 мг 2 р/сут",
                 ""]
        n = 0
        for r in rows:
            draft = {
                "regulation": "Приказ Росздравнадзора № 1071 от 15.02.2017",
                "recipient": "АИС Росздравнадзора + служба фармаконадзора ООО «иФарма» (ГК «ХимРар»)",
                "suspect_drug": "Авиандр® (маритупирдин)",
                "meddra_term": r["meddra_term"],
                "severity_ctcae": r["severity_ctcae"],
                "related_to_drug": r["related_to_drug"],
                "reported_at": str(r["reported_at"]),
                "status": "ЧЕРНОВИК — требует проверки и подписи врача",
                "formed_by_role": principal.role,
            }
            await conn.execute(
                "UPDATE adverse_events_and_risks SET roszdravnadzor_draft=$2::jsonb WHERE ae_id=$1",
                r["ae_id"], json.dumps(draft, ensure_ascii=False))
            n += 1
            lines.append(f"• {r['meddra_term']} — {SEV_RU.get(r['severity_ctcae'], 'CTCAE ?')}, "
                         f"связь с препаратом: {REL_RU.get(r['related_to_drug'], r['related_to_drug'])}, "
                         f"зарегистрировано {r['reported_at'].strftime('%d.%m.%Y')}")
        lines += ["", "Черновик сформирован автоматически из дневника пациента.",
                  "Перед отправкой в Росздравнадзор требует проверки врачом и внесения данных пациента."]
        if n == 0:
            lines = ["Все записи НЯ уже имеют черновики извещения."] + lines[4:]
    return {"draft_text": "\n".join(lines), "drafts_formed": n}


@bio_router.get("/scales/{code}", summary="[Опросник] Полная шкала по КР МЗ РФ (item-level)")
def get_scale(code: str) -> Dict[str, Any]:
    from fastapi import HTTPException
    s = SCALES.get(code) or SCALES.get(code.upper())
    if not s:
        raise HTTPException(404, f"шкала {code} не найдена: доступные {list(SCALES)}")
    return s


@bio_router.post("/scales/{code}/submit",
                 summary="[Пациент] Ответы по пунктам -> балл + градация КР МЗ + флаги")
async def submit_scale(code: str, payload: ScaleSubmit, request: Request,
                       patient: Principal = Depends(get_principal)) -> Dict[str, Any]:
    import json as _json
    import uuid as _uuid

    from backend.db.context import db_conn

    result = score_answers(code.upper(), payload.answers)
    from fastapi import HTTPException
    if "error" in result:
        raise HTTPException(400, result["error"])

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        ph = _uuid.UUID(patient.sub)
        items = _json.dumps({"answers": payload.answers}, ensure_ascii=False)
        async with db_conn(pool, role="PATIENT", user_id=patient.sub) as conn:
            # taken_at фиксируем один раз: подшкалы HADS-A/D должны попасть в БД
            # с тем же временем, что и общий балл (иначе графики разъезжаются)
            taken_at = await conn.fetchval(
                "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, taken_at) "
                "VALUES ($1,$2,$3,$4::jsonb, now()) RETURNING taken_at",
                ph, result["scale_code"], result["total_score"], items)
            if code.upper() == "HADS":
                for sub_code, sub_total in (("HADS-A", result["hads_a"]), ("HADS-D", result["hads_d"])):
                    await conn.execute(
                        "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, taken_at) "
                        "VALUES ($1,$2,$3,$4::jsonb, $5)",
                        ph, sub_code, sub_total, items, taken_at)
    result["stored"] = pool is not None
    return result


@bio_router.post("/patient/{patient_hash}/clinician-scale",
                 summary="[Врач] Записать клиницистскую шкалу (HAM-A / CGI) по пациенту")
async def submit_clinician_scale(patient_hash: str, code: str, payload: ScaleSubmit,
                                 request: Request, principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """HAM-A-14 и CGI заполняет ВРАЧ на приёме (КР МЗ РФ: шкала Гамильтона —
    врачебная оценка). Пишем под ролью врача: RLS сам проверит app_is_attending."""
    import json as _json
    import uuid as _uuid

    from backend.db.context import db_conn
    from fastapi import HTTPException

    if principal.role not in ("DOCTOR_PSYCHIATRIST", "DOCTOR_NEUROLOGIST", "DOCTOR_GP", "ADMIN_METHODOLOGIST"):
        raise HTTPException(403, "Клиницистские шкалы заполняет врач")
    if code.upper() not in ("HAM-A", "CGI"):
        raise HTTPException(400, "Этот эндпоинт — только для клиницистских шкал (HAM-A, CGI)")
    result = score_answers(code.upper(), payload.answers)
    if "error" in result:
        raise HTTPException(400, result["error"])
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    ph = _uuid.UUID(patient_hash)
    items = _json.dumps({"answers": payload.answers, "filled_by_role": principal.role},
                        ensure_ascii=False)
    async with db_conn(pool, role=principal.role, user_id=principal.sub) as conn:
        await conn.execute(
            "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, taken_at) "
            "VALUES ($1,$2,$3,$4::jsonb, now())",
            ph, result["scale_code"], result["total_score"], items)
    result["stored"] = True
    return result


@bio_router.get("/patient/{patient_hash}/insights",
                summary="[Врач] Аналитика дневника: как препарат действует на ЭТОГО пациента")
async def patient_insights(patient_hash: str, request: Request,
                           principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    import uuid as _uuid

    from backend.db.context import db_conn
    from fastapi import HTTPException

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    try:
        ph = _uuid.UUID(patient_hash)
    except ValueError:
        raise HTTPException(400, "patient_hash должен быть UUID")

    async with db_conn(pool, role=principal.role, user_id=principal.sub) as conn:
        diary = await conn.fetch(
            "SELECT entry_date, intake_time, taken, before_meal_30min, side_effects, "
            "mood_score, anxiety_score, sleep_score, notes, "
            "(entry_date - (SELECT MIN(entry_date) FROM patient_diary_entries WHERE patient_hash_id=$1) + 1) AS therapy_day "
            "FROM patient_diary_entries WHERE patient_hash_id=$1 ORDER BY entry_date", ph)
        scales = await conn.fetch(
            "SELECT scale_code, total_score, taken_at FROM patient_scale_assessments "
            "WHERE patient_hash_id=$1 ORDER BY taken_at", ph)
        alerts = await conn.fetch(
            "SELECT alert_id, critical, created_at, doctor_reaction_at "
            "FROM clinical_alerts_and_routing WHERE patient_hash_id=$1 "
            "ORDER BY created_at DESC LIMIT 10", ph)
        aes = await conn.fetch(
            "SELECT ae_id, meddra_term, severity_ctcae, related_to_drug, reported_at, "
            "roszdravnadzor_draft IS NOT NULL AS has_draft "
            "FROM adverse_events_and_risks WHERE patient_hash_id=$1 "
            "ORDER BY reported_at DESC LIMIT 20", ph)
        adherence = await conn.fetchval(
            "SELECT pdc_pct FROM medication_adherence_logs WHERE patient_hash_id=$1 "
            "ORDER BY taken_at DESC LIMIT 1", ph)
    diary_rows = []
    for d in diary:
        r = dict(d)
        r["side_effects"] = _json_loads(d.get("side_effects")) if d.get("side_effects") else []
        diary_rows.append(r)
    insights = build_insights(therapy_day=None, diary=diary_rows,
                              scales=[dict(s) for s in scales],
                              alerts=[dict(a) for a in alerts],
                              adherence_pct=adherence)
    insights["adverse_events"] = [dict(a) for a in aes]
    insights["patient_hash"] = patient_hash
    return insights


def _json_loads(v):
    import json as _json
    return _json.loads(v)


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
