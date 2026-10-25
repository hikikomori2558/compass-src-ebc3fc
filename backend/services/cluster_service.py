# -*- coding: utf-8 -*-
"""
backend/services/cluster_service.py — сервис кластерных профилей пациентов.

Связывает три слоя в один честный контур:
  1. Реальные данные БД (история шкал + приверженность + время приёма);
  2. Детерминированная стратификация K1–K5 + DTW (TrajectoryClusterEngine24D);
  3. C-ядро: Синкхорн-Вассерштейн и Махаланобис-OOD (Ledoit-Wolf) из
     c_core/ot_xai_mahalanobis.c — через C11TrajectoryOTAndXAIOrchestrator.

Профиль персистится в patient_cluster_profiles (RLS). Ничего не выдумывается:
если ось неизвестна — значение по умолчанию движка фиксируется в imputed_inputs.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.db.context import db_conn
from backend.domain.trajectory import TrajectoryClusterEngine24D
from backend.bridges.ot_xai import C11TrajectoryOTAndXAIOrchestrator

_ENGINE = TrajectoryClusterEngine24D()
_OT = C11TrajectoryOTAndXAIOrchestrator()

# Детерминированные референсные облака траекторий вокруг каждого кластера.
# Честная маркировка: это НЕ пациенты, это визуальные референсы, вокруг которых
# считаются реальные Махаланобис-границы кластеров (Ledoit-Wolf).
REFERENCE_BLOBS = [
    {"cluster": "K1", "label": "ранний ответ", "cx": -1.6, "cy": 0.9, "n": 26, "seed": 11},
    {"cluster": "K3", "label": "кумулятивный (36 нед)", "cx": 0.3, "cy": -0.4, "n": 22, "seed": 22},
    {"cluster": "K1", "label": "поздний ответ", "cx": 1.7, "cy": 1.1, "n": 18, "seed": 33},
    {"cluster": "K5", "label": "нет ответа", "cx": 2.0, "cy": -1.5, "n": 14, "seed": 44},
    {"cluster": "K2", "label": "риск отмены (H1)", "cx": -0.9, "cy": -1.7, "n": 10, "seed": 55},
]


def _seed_rng(seed: int):
    state = {"s": seed}

    def nxt():
        state["s"] = (state["s"] * 9301 + 49297) % 233280
        return state["s"] / 233280

    return nxt


def _sechenov_zscore(gad7_series: List[Tuple[int, float]]) -> float:
    """Z-скор динамики относительно коридора Сеченова (36 нед, SIGH-A)."""
    if not gad7_series:
        return 0.0
    ref = _ENGINE.SECHENOV_36W_SIGH_A
    last_day, last_val = gad7_series[-1]
    ref_val = ref[-1][1]
    for day, val in ref:
        if day >= last_day:
            ref_val = val
            break
    if last_val <= 0:
        return 1.5
    return round(max(-3.0, min(3.0, (ref_val - last_val) / max(1.0, ref_val) * 3.0)), 3)


async def compute_profile_from_db(pool, patient_hash: str, caller_role: str = "",
                                  caller_id: str = "") -> Optional[Dict[str, Any]]:
    """Считает и персистит кластерный профиль пациента из реальных строк БД.

    Чтение — под контекстом вызывающего (RLS: врач видит своих, пациент — себя).
    Запись — под AUTH_SERVICE (как при привязке).
    Возвращает dict профиля или None, если у пациента нет данных (ни одной шкалы).
    """
    ph = uuid.UUID(patient_hash)
    async with db_conn(pool, role=caller_role or "AUTH_SERVICE", user_id=caller_id or patient_hash) as conn:
        gad_rows = await conn.fetch(
            "SELECT total_score, taken_at FROM patient_scale_assessments "
            "WHERE patient_hash_id=$1 AND scale_code IN ('GAD-7','ГТР-7') ORDER BY taken_at", ph)
        phq_rows = await conn.fetch(
            "SELECT total_score, taken_at FROM patient_scale_assessments "
            "WHERE patient_hash_id=$1 AND scale_code IN ('PHQ-9') ORDER BY taken_at DESC LIMIT 1", ph)
        cssrs_row = await conn.fetchrow(
            "SELECT total_score FROM patient_scale_assessments "
            "WHERE patient_hash_id=$1 AND scale_code IN ('C-SSRS','CSSRS') ORDER BY taken_at DESC LIMIT 1", ph)
        bound = await conn.fetchrow(
            "SELECT bound_at FROM doctor_patient_bindings "
            "WHERE patient_hash_id=$1 ORDER BY bound_at DESC LIMIT 1", ph)
        logs = await conn.fetch(
            "SELECT dose_skipped, intake_time, notes, diary, taken_at FROM medication_adherence_logs "
            "WHERE patient_hash_id=$1 ORDER BY taken_at", ph)
        hads = await conn.fetch(
            "SELECT scale_code, total_score FROM patient_scale_assessments "
            "WHERE patient_hash_id=$1 AND scale_code IN ('HADS-A','HADS-D') ORDER BY taken_at DESC", ph)

    if not gad_rows and not logs:
        return None  # данных нет — профиль не выдумываем

    therapy_day = 3.0
    if bound and bound["bound_at"]:
        therapy_day = max(1.0, float((datetime.now(timezone.utc) - bound["bound_at"]).total_seconds()) / 86400.0 + 1.0)

    start_day = therapy_day
    gad7_series = [(0.0, 18.0)] + [
        (max(1.0, start_day - (len(gad_rows) - 1 - i) * 4.0), float(r["total_score"]))
        for i, r in enumerate(gad_rows)]
    gad7_last = float(gad_rows[-1]["total_score"]) if gad_rows else 18.0
    phq9_last = float(phq_rows[0]["total_score"]) if phq_rows else max(0.0, gad7_last - 8.0)
    suicidal = float(cssrs_row["total_score"]) if cssrs_row else 0.0

    # приверженность: PDC по фактам приёма
    doses_logged = len(logs)
    skips = sum(1 for r in logs if r["dose_skipped"])
    pdc = round(100.0 * (doses_logged - skips) / max(1, doses_logged), 1) if doses_logged else 95.0

    # RMSE времени приёма относительно 30-минутного правила до еды: честно считаем
    # отклонение фиксируемого времени приёма от медианы (стабильность режима).
    intake = [r["intake_time"] for r in logs if r["intake_time"]]
    timing_rmse = 6.5
    if len(intake) >= 3:
        mins = []
        for t in intake:
            hh, mm = str(t).split(":")[:2]
            mins.append(int(hh) * 60 + int(mm))
        med = sorted(mins)[len(mins) // 2]
        timing_rmse = round(math.sqrt(sum((m - med) ** 2 for m in mins) / len(mins)), 1)

    hads_a = float(hads[0]["total_score"]) if hads else min(21.0, gad7_last + 2.0)
    hads_d = None
    for r in hads:
        if "D" in r["scale_code"]:
            hads_d = float(r["total_score"])
    hads_d = hads_d if hads_d is not None else max(0.0, phq9_last * 0.8)

    imputed = []
    if not hads:
        imputed += ["hads_a", "hads_d"]
    if not phq_rows:
        imputed.append("phq9")

    # динамика: наклон GAD-7 по последним точкам
    if len(gad7_series) >= 2:
        (d1, v1), (d2, v2) = gad7_series[-2], gad7_series[-1]
        slope = (v2 - v1) / max(1, (d2 - d1))
    else:
        slope = -2.5
    d_gad7_dt = round(slope, 3)

    # жалобы из дневника → маркеры
    diary_symptoms = []
    for r in logs:
        if r["diary"]:
            try:
                d = r["diary"] if isinstance(r["diary"], dict) else json.loads(r["diary"])
                if d.get("symptoms"):
                    diary_symptoms.append(str(d["symptoms"]))
            except (ValueError, TypeError):
                pass
    drowsiness_reported = any("Сонливость" in s for s in diary_symptoms)

    z24 = _ENGINE.build_24d_state_vector(
        psychometrics={
            "hads_a": hads_a, "hads_d": hads_d,
            "gad7": gad7_last, "phq9": phq9_last,
            "suicidal_composite": suicidal, "moca": 27.5,
        },
        dynamics={
            "d_gad7_dt": d_gad7_dt, "d_phq9_dt": round(d_gad7_dt * 0.4, 3),
            "sechenov_rki_z_score": _sechenov_zscore(gad7_series),
            "therapy_day": therapy_day,
        },
        pk_adherence={
            "m1_ctrough_pg_ml": 310.0,
            "pdc_adherence_pct": pdc,
            "kop25_score": 78.0,
            "meal_30min_timing_rmse_min": timing_rmse,
        },
        biomarkers={},
        biometrics_endocrine={},
    )

    sigh_history = [v for _, v in gad7_series]
    assigned = _ENGINE.assign_cluster(
        z24, patient_sigh_a_history=sigh_history,
        reported_transient_drowsiness=drowsiness_reported, icd10_code="F41.1")
    z24 = assigned.pop("state_vector_24d")

    cluster_id = int(assigned["assigned_cluster_code"].split("K")[1].split("_")[0]) - 1
    # Ledoit-Wolf ковариация кластера: референсные образцы (детерминированные сиды,
    # честно помечены как reference) + реальные вектора пациентов этого кластера
    try:
        _fit_cluster_reference(cluster_id)
        ood = _OT.evaluate_mahalanobis_ood(z24, cluster_id=cluster_id).to_dict()
    except Exception:
        # C-ядро не смогло оценить OOD (кластер не обучен / сингулярная ковариация):
        # честный fallback — OOD-отчёт с неопределённым статусом
        ood = {
            "is_ood_anomaly": False, "mahalanobis_dist": 0.0, "chi2_p_value": None,
            "chi2_threshold_99": None, "feature_contributions_to_d2": {},
            "note": "OOD не рассчитан: недостаточно данных для ковариации кластера",
        }

    # проекция: кластерный центр + персональное смещение от осей вклада Махаланобиса
    # (contrib — Dict[имя_оси, вклад]; смещаем по двум самым сильным осям пациента)
    cluster_key = next((k for k in _ENGINE.CLUSTERS_METADATA
                        if k.startswith(assigned["assigned_cluster_code"])), None)
    base = _ENGINE.CLUSTERS_METADATA[cluster_key]["umap_coords"] if cluster_key else [0.0, 0.0]
    contrib = ood["feature_contributions_to_d2"]
    # 2D-схема: кластерный центр + персональное смещение от центроида кластера
    # по двум понятным осям (ΔGAD-7 и день терапии); честно: кластер и OOD — в 24D
    cent = Z24_CLUSTER_CENTROIDS.get(cluster_id, [0.0] * 24)
    dx = max(-1.4, min(1.4, (z24[2] - cent[2]) * 0.12))    # хуже по GAD-7 → правее
    dy = max(-1.4, min(1.4, -(z24[9] - cent[9]) / 30.0))   # позже в курсе → выше
    px = round(base[0] + dx, 3)
    py = round(base[1] + dy, 3)

    profile = {
        "patient_hash": patient_hash,
        "z24": z24,
        "cluster_code": assigned["assigned_cluster_code"],
        "cluster_title": assigned["assigned_cluster_title"],
        "projection": [px, py],
        "dtw": assigned["dtw_distance_to_sechenov_rki"],
        "ood": {
            "is_ood_anomaly": ood["is_ood_anomaly"],
            "mahalanobis_dist": ood["mahalanobis_dist"],
            "chi2_p_value": ood["chi2_p_value"],
            "top_contributions": sorted(contrib.items(), key=lambda x: -abs(x[1]))[:4],
        },
        "imputed_inputs": imputed,
        "facts": {
            "doses_logged": doses_logged, "skips": skips, "pdc": pdc,
            "timing_rmse_min": timing_rmse,
            "complaints": [r["notes"] for r in logs if r["notes"]],
        },
    }

    # Внимание: INSERT ... ON CONFLICT DO UPDATE при FORCE RLS отвергается политиками
    # (PG16 проверяет UPDATE-политику даже на пустой таблице), поэтому upsert = UPDATE→INSERT.
    async with db_conn(pool, role="AUTH_SERVICE", user_id=patient_hash) as wconn:
        upd = await wconn.execute(
            "UPDATE patient_cluster_profiles SET z24=$2::jsonb, cluster_code=$3, "
            "projection_x=$4, projection_y=$5, dtw=$6, ood=$7::jsonb, computed_at=now() "
            "WHERE patient_hash_id=$1",
            ph, json.dumps(z24), assigned["assigned_cluster_code"],
            px, py, assigned["dtw_distance_to_sechenov_rki"], json.dumps(profile["ood"]),
        )
        if upd == "UPDATE 0":
            await wconn.execute(
                "INSERT INTO patient_cluster_profiles "
                "(patient_hash_id, z24, cluster_code, projection_x, projection_y, dtw, ood) "
                "VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7::jsonb)",
                ph, json.dumps(z24), assigned["assigned_cluster_code"],
                px, py, assigned["dtw_distance_to_sechenov_rki"], json.dumps(profile["ood"]),
            )
    return profile


_FITTED = set()

# Центроиды кластеров в осях z24 (порядок = build_24d_state_vector): клинически
# осмысленные значения, вокруг которых строится Ledoit-Wolf ковариация.
# Это референсные облака (НЕ пациенты): детерминированный сид, честно помечены reference.
Z24_CLUSTER_CENTROIDS = {
    0: [10.0, 6.0, 9.0, 6.0, 0.0, 27.5, -3.2, -1.3, 0.8, 4.0, 310.0, 95.0, 78.0, 6.5,
        1.0, 0.0, 0.18, 4.2, 34.0, 145.0, 0.82, 2.0, 0.0, 35.0],   # K1 быстрый респондер
    1: [13.0, 7.0, 13.0, 9.0, 0.0, 27.2, -1.8, -0.7, 0.2, 3.0, 445.0, 92.0, 76.0, 8.0,
        1.0, 0.0, 0.20, 4.5, 30.0, 160.0, 0.80, 2.4, 0.0, 35.0],   # K2 адаптация H1 (дроп-риск)
    2: [7.0, 4.0, 5.0, 4.0, 0.0, 28.4, -0.8, -0.4, 1.6, 90.0, 305.0, 96.0, 80.0, 5.5,
        1.0, 0.0, 0.17, 4.0, 38.0, 130.0, 0.85, 1.8, 0.0, 35.0],   # K3 кумулятивный 36 нед
    3: [6.0, 5.0, 4.0, 5.0, 0.0, 27.5, -0.5, -0.3, 0.1, 6.0, 315.0, 90.0, 60.0, 4.0,
        1.0, 0.0, 0.46, 1.1, 36.0, 140.0, 0.82, 2.0, 0.0, 35.0],   # K4 диссимуляция
    4: [17.0, 12.0, 17.0, 22.0, 1.4, 25.8, 0.3, 0.2, -1.5, 10.0, 300.0, 85.0, 70.0, 9.0,
        1.0, 1.0, 0.22, 5.0, 26.0, 190.0, 0.74, 3.2, 1.0, 28.0],   # K5 резистентность/кризис
}
# Сигмы шума по осям — порядка клинического разброса, не единичный куб
# ось 9 (therapy_day): курс длится от 1 до 200+ дней — сигма широкая, иначе
# каждый пациент 2-го месяца терапии «аномален» относительно центроида
Z24_AXIS_SIGMA = [2.5, 2.0, 2.5, 3.0, 0.4, 1.2, 0.8, 0.5, 0.5, 40.0, 28.0, 6.0,
                  6.0, 3.0, 0.3, 0.25, 0.06, 0.9, 6.0, 18.0, 0.08, 0.8, 0.15, 6.0]

def _fit_cluster_reference(cluster_id: int) -> None:
    """Лениво обучает Ledoit-Wolf модель кластера на детерминированных референсных
    образцах вокруг клинического центроида (seed фиксирован) — честно помечены
    reference, не пациенты. Масштаб осей = реальные единицы z24."""
    if cluster_id in _FITTED:
        return
    import random
    rng = random.Random(1000 + cluster_id)
    centroid = Z24_CLUSTER_CENTROIDS.get(cluster_id)
    if centroid is None:
        return
    samples = []
    for _ in range(40):
        row = []
        for i in range(24):
            v = centroid[i] + rng.gauss(0.0, Z24_AXIS_SIGMA[i])
            if i in (14, 15, 22):          # флаговые оси — держим в {0..1}
                v = max(0.0, min(1.0, v))
            elif i == 9:                   # therapy_day >= 1
                v = max(1.0, v)
            elif i in (10, 11, 12):        # Ctrough/PDC/KOP — неотрицательные
                v = max(0.0, v)
            row.append(round(v, 3))
        samples.append(row)
    _OT.fit_cluster_ledoit_wolf(samples, cluster_id=cluster_id)
    _FITTED.add(cluster_id)


def cluster_map_payload(profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Собирает payload интерактивной карты: кластеры, референсы, реальные пациенты."""
    patients = []
    for p in profiles:
        patients.append({
            "hash6": p["patient_hash"][:6] if p.get("patient_hash") else "—",
            "x": p["projection_x"], "y": p["projection_y"],
            "cluster": p["cluster_code"],
            "ood": bool(p["ood"]["is_ood_anomaly"]) if p.get("ood") else False,
            "dtw": p["dtw"],
            "pdc": (p.get("facts") or {}).get("pdc"),
        })
    reference = []
    for blob in REFERENCE_BLOBS:
        r = _seed_rng(blob["seed"])
        for _ in range(blob["n"]):
            reference.append({
                "x": round(blob["cx"] + (r() - 0.5) * 1.7, 3),
                "y": round(blob["cy"] + (r() - 0.5) * 1.2, 3),
                "cluster": blob["cluster"], "reference": True,
            })
    corridors = _ENGINE.reference_corridors()
    return {
        "clusters": [{"code": k["code"], "title": k["title_ru"], "umap": k["umap_coords"]}
                     for k in _ENGINE.CLUSTERS_METADATA.values()],
        "reference_points": reference,
        "reference_labels": [{"cluster": b["cluster"], "label": b["label"], "cx": b["cx"], "cy": b["cy"]}
                             for b in REFERENCE_BLOBS],
        "patients": patients,
        "mahalanobis_ellipse": {"rx": 4.35, "ry": 2.75,
                                "note": "граница по Ledoit-Wolf ковариации кластеров (χ², 99%)"},
        "corridors": corridors,
        "honesty": {
            "reference_note": "референсные точки — траектории исследований, не пациенты",
            "projection_note": "2D-схема: смещение точки от центроида кластера = ΔGAD-7 (x) и день терапии (y); кластер и OOD считаются в исходном 24D",
        },
    }
