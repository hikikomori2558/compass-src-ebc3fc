# -*- coding: utf-8 -*-
"""
scripts/seed_105_patients.py — материализация 105 рукописных персон в БД стенда.

Персоны заданы ЯВНО в synthetic_personas.py (каждая — отдельная строка с историей,
никакого генератора). Здесь только детерминированный рендер:
дневник/шкалы/приёмы вычисляются из траектории конкретной персоны.

Что попадает в БД по каждому пациенту:
  vault(is_synthetic) + привязка с ярлыком + план терапии (у С-050 — оверрайд ВК)
  + разбор ЭМК (МКБ для кластер-правил) + GAD-7 еженедельно + HADS/A/D + HAM-A
  (точки 0/2/4/8 нед) + CGI + (K5: PHQ-9 с п.9 и C-SSRS + критический алерт)
  + ежедневные приёмы (intake_time, пропуски, жалобы) + дневник состояния
  + НЯ (часть — с черновиком 1071) + переписка (15 персон) + вовлечённость.

Запуск (в контейнере api; owner-DSN обходит RLS только для заливки):
  PW=$(grep POSTGRES_PASSWORD .env | cut -d= -f2)
  docker compose exec -e OWNER_DATABASE_URL="postgresql://compass_app:$PW@db:5432/compass" \
      api python -m scripts.seed_105_patients
Идемпотентно: синтетический набор пересоздаётся целиком (uuid5 стабильны).
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from synthetic_personas import MSG_QA, PERSONAS  # noqa: E402

OWNER_DSN = os.getenv(
    "OWNER_DATABASE_URL",
    "postgresql://compass_app:compass_demo@localhost:5433/compass",
)

NS = uuid.uuid5(uuid.NAMESPACE_URL, "compass-synthetic-2026")
SYM2MEDDRA = {
    "Сонливость днём": "Сонливость (Somnolence)",
    "Сонливость к вечеру": "Сонливость (Somnolence)",
    "Тошнота": "Тошнота (Nausea)",
    "Головокружение": "Головокружение (Dizziness)",
    "Головная боль": "Головная боль (Headache)",
}
SYM2CODE = {
    "Сонливость днём": "sleepy", "Сонливость к вечеру": "sleepy",
    "Тошнота": "nausea", "Головокружение": "dizzy", "Головная боль": "headache",
}


def ph(n: int) -> uuid.UUID:
    return uuid.uuid5(NS, f"synthetic-{n:03d}")


def ev(n: int, day: int) -> uuid.UUID:
    return uuid.uuid5(NS, f"synthetic-{n:03d}-day-{day:03d}")


def intake_time(p: dict, day: int) -> str:
    if p["it"] == "M":
        return "08:{:02d}".format(5 + (p["n"] * 7) % 40)
    if p["it"] == "E":
        return "{:02d}:{:02d}".format(19 + (p["n"] % 3), (p["n"] * 11) % 60)
    return "08:{:02d}".format(5 + (p["n"] * 7) % 40) if day % 2 == 0 else \
           "{:02d}:{:02d}".format(19 + (p["n"] % 3), (p["n"] * 11) % 60)


def sleep_bucket(gad: int) -> str:
    return "8+ ч" if gad <= 6 else "6–7 ч" if gad <= 13 else "4–5 ч"


def gad_at(p: dict, week: int) -> int:
    g = p["gad"]
    i = min(week, len(g) - 1)
    return g[i]


async def main() -> int:
    conn = await asyncpg.connect(OWNER_DSN)
    stats = {"patients": 0, "scales": 0, "intakes": 0, "diary": 0, "alerts": 0,
             "ae": 0, "msgs": 0, "critical_unconfirmed": 0, "overrides": 0, "ood_personas": 0}
    try:
        neuro = await conn.fetchval("SELECT user_id FROM users_auth WHERE login='neuro.ivlanov'")
        psych = await conn.fetchval("SELECT user_id FROM users_auth WHERE login='psych.petrova'")
        assert neuro and psych, "нет демо-врачей — сначала python -m backend.db.seed"

        syn = [ph(p["n"]) for p in PERSONAS]
        # --- идемпотентность: снести прошлый синтетический набор
        for tbl in ("patient_cluster_profiles", "clinical_alerts_and_routing",
                    "adverse_events_and_risks", "messages", "patient_engagement",
                    "patient_diary_entries", "medication_adherence_logs",
                    "patient_scale_assessments", "therapy_plans_and_prescriptions",
                    "ehr_encounters_parsed", "doctor_patient_bindings"):
            await conn.execute(
                f"DELETE FROM {tbl} WHERE patient_hash_id = ANY($1::uuid[])", syn)
        await conn.execute(
            "DELETE FROM audit_log WHERE entity = 'patient' AND entity_id = ANY($1::text[])",
            [str(x) for x in syn])
        await conn.execute(
            "DELETE FROM patient_pii_vault WHERE patient_hash_id = ANY($1::uuid[])", syn)

        for p in PERSONAS:
            n, P = p["n"], ph(p["n"])
            d = p["d"]
            gad = p["gad"]
            weeks = len(gad)
            flags = p["f"]
            drop = next((int(x.split(":")[1]) for x in flags if x.startswith("drop:")), None)
            active_days = drop if drop else d
            is_k5 = n >= 98
            doctor = psych if is_k5 else neuro
            birth = date(p["b"], (n % 12) + 1, (n % 27) + 1)
            bound_at = datetime.now(timezone.utc) - timedelta(days=d)

            # --- vault + привязка (ярлык персоны — жюри видит его в когорте) ---
            await conn.execute(
                "INSERT INTO patient_pii_vault (patient_hash_id, fio_encrypted, phone_encrypted, "
                "snils_encrypted, birth_date, sex, key_version, is_synthetic) "
                "VALUES ($1,$2,$3,$4,$5,$6,0,true)",
                P, b"SYN-FIO-" + P.bytes[:8], b"SYN-PHONE-" + P.bytes[:6],
                b"SYN-SNILS-" + P.bytes[:6], birth, p["s"])
            await conn.execute(
                "INSERT INTO doctor_patient_bindings (doctor_id, patient_hash_id, consents, bound_at) "
                "VALUES ($1,$2,$3,$4)", doctor, P,
                json.dumps({"label": p["l"], "synthetic": True,
                            "type": "ИДС_телемониторинг", "signed_at": bound_at.isoformat(),
                            "method": "QR"}, ensure_ascii=False), bound_at)
            await conn.execute(
                "INSERT INTO therapy_plans_and_prescriptions "
                "(patient_hash_id, doctor_id, drug_code, label_status, dosing, confirmed_by_doctor_id, "
                "confirmed_at, status) VALUES ($1,$2,'AVIANDR','IN_LABEL',$3,$2,$4,'ACTIVE')",
                P, doctor, json.dumps({"scheme": "20 мг 2 р/сут", "before_meal_min": 30}),
                bound_at)

            # --- разбор ЭМК: МКБ для кластер-правил (K1 = F43.2/R45.7/Z60.0) ---
            egfr = 22 if "override-ckd" in flags else 62 + (n * 13) % 50
            await conn.execute(
                "INSERT INTO ehr_encounters_parsed (patient_hash_id, doctor_id, raw_text_hash, "
                "parsed, icd10_primary, created_at) VALUES ($1,$2,$3,$4::jsonb,$5,$6)",
                P, doctor, f"synthetic-{n:03d}",
                json.dumps({"icd10": p["icd"], "cyp2d6": "NM", "egfr": egfr,
                            "synthetic": True}, ensure_ascii=False),
                p["icd"], bound_at)

            # --- шкалы ---
            for k, score in enumerate(gad):  # GAD-7 еженедельно
                await conn.execute(
                    "INSERT INTO patient_scale_assessments "
                    "(patient_hash_id, scale_code, total_score, items, taken_at) "
                    "VALUES ($1,'GAD-7',$2,$3::jsonb, now() - make_interval(days => $4::int))",
                    P, score,
                    json.dumps({"source": "PWA_checkin", "synthetic": True, "week": k}),
                    max(0, d - k * 7))
                stats["scales"] += 1
            start_a = min(21, p["ha"] + 4)
            start_d = min(21, p["hd"] + 3)
            for k in range(0, weeks, 2):  # HADS каждые 2 недели, линейно к последнему
                t = k / max(1, weeks - 1)
                ha_k = round(start_a + (p["ha"] - start_a) * t)
                hd_k = round(start_d + (p["hd"] - start_d) * t)
                row = json.dumps({"answers": "synthetic", "synthetic": True})
                for code, sc in (("HADS", ha_k + hd_k), ("HADS-A", ha_k), ("HADS-D", hd_k)):
                    await conn.execute(
                        "INSERT INTO patient_scale_assessments "
                        "(patient_hash_id, scale_code, total_score, items, taken_at) "
                        "VALUES ($1,$2,$3,$4::jsonb, now() - make_interval(days => $5::int))",
                        P, code, sc, row, max(0, d - k * 7))
                    stats["scales"] += 1
            for wk in (0, 2, 4, 8):  # HAM-A врача, точки КР
                if weeks >= wk:
                    t = wk / max(1, weeks - 1)
                    v = round(p["h0"] + (p["hL"] - p["h0"]) * t)
                    await conn.execute(
                        "INSERT INTO patient_scale_assessments "
                        "(patient_hash_id, scale_code, total_score, items, taken_at) "
                        "VALUES ($1,'HAM-A',$2,$3::jsonb, now() - make_interval(days => $4::int))",
                        P, v,
                        json.dumps({"filled_by_role": "DOCTOR", "synthetic": True}),
                        max(0, d - wk * 7))
                    stats["scales"] += 1
            end_gad = gad[-1]  # CGI врача на последней точке
            cgi_s = 2 if end_gad <= 5 else 3 if end_gad <= 8 else 4 if end_gad <= 11 else \
                5 if end_gad <= 14 else 6 if end_gad <= 17 else 7
            cgi_i = max(1, cgi_s - 2) if end_gad <= gad[0] else min(7, cgi_s + 1)
            await conn.execute(
                "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, "
                "items, taken_at) VALUES ($1,'CGI',$2,$3::jsonb, now() - make_interval(days => 1))",
                P, cgi_s + cgi_i,
                json.dumps({"filled_by_role": "DOCTOR", "cgi_s": cgi_s, "cgi_i": cgi_i,
                            "synthetic": True}))
            stats["scales"] += 1

            if is_k5:  # PHQ-9 (тяжёлый, п.9 позитивный) + C-SSRS + критический алерт
                phq = 20 + (n % 4)
                cssrs = 2 + (n % 3)
                await conn.execute(
                    "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, "
                    "total_score, items, taken_at) VALUES ($1,'PHQ-9',$2,$3::jsonb, "
                    "now() - make_interval(days => 7))",
                    P, phq, json.dumps({"item9": 1, "synthetic": True}))
                await conn.execute(
                    "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, "
                    "total_score, items, taken_at) VALUES ($1,'C-SSRS',$2,$3::jsonb, "
                    "now() - make_interval(days => 7))",
                    P, cssrs, json.dumps({"synthetic": True}))
                stats["scales"] += 2
                reasons = json.dumps([f"C-SSRS = {cssrs}", f"PHQ-9 = {phq} (п.9 = 1)"],
                                     ensure_ascii=False)
                if "crit-unconf" in flags:  # живой красный баннер врачу
                    await conn.execute(
                        "INSERT INTO clinical_alerts_and_routing (patient_hash_id, category, "
                        "critical, reasons, delivery_ms) VALUES "
                        "($1,'MANDATORY_PSYCHIATRIST',true,$2::jsonb, 121)", P, reasons)
                    stats["critical_unconfirmed"] += 1
                else:
                    await conn.execute(
                        "INSERT INTO clinical_alerts_and_routing (patient_hash_id, category, "
                        "critical, reasons, delivery_ms, delivered_to_doctor_at, "
                        "doctor_reaction_at, action_taken) VALUES "
                        "($1,'MANDATORY_PSYCHIATRIST',true,$2::jsonb, 108, "
                        "now() - make_interval(days => 7), now() - make_interval(days => 6), "
                        "'Телефонный контакт; психиатр, госпитализация согласована')",
                        P, reasons)
                stats["alerts"] += 1

            # --- ежедневный контур: приёмы + дневник + жалобы ---
            complaints = {day: chip for day, chip in p["c"]}
            skipset = set(p["sk"])
            streak = best = 0
            for day in range(active_days):
                d_ago = d - day
                skipped = day in skipset
                streak = 0 if skipped else streak + 1
                best = max(best, streak)
                week = min(day // 7, weeks - 1)
                g_cur = gad[week]
                chip = complaints.get(day + 1)  # жалобы в c заданы с 1
                diary = None
                if not skipped:
                    diary = {"sleep": sleep_bucket(g_cur),
                             "energy": max(1, 4 - g_cur // 7),
                             "symptoms": ([chip] if chip else [])}
                it = intake_time(p, day)
                await conn.execute(
                    "INSERT INTO medication_adherence_logs "
                    "(patient_hash_id, planned_at, taken_at, dose_skipped, intake_time, notes, "
                    "diary, event_uuid) VALUES ($1, now() - make_interval(days => $2::int), "
                    "now() - make_interval(days => $2::int), $3, $4, $5, $6::jsonb, $7) "
                    "ON CONFLICT (event_uuid) DO NOTHING",
                    P, d_ago, skipped, None if skipped else it, chip,
                    json.dumps(diary, ensure_ascii=False) if diary else None, ev(n, day))
                stats["intakes"] += 1
                se = [{"code": SYM2CODE[chip], "severity": 1}] if chip else []
                await conn.execute(
                    "INSERT INTO patient_diary_entries (patient_hash_id, entry_date, intake_time, "
                    "taken, before_meal_30min, side_effects, mood_score, anxiety_score, "
                    "sleep_score, notes) VALUES ($1, current_date - make_interval(days => $2::int), "
                    "$3, $4, $5, $6::jsonb, $7, $8, $9, $10)",
                    P, d_ago,
                    (None if skipped else dtime(int(it[:2]), int(it[3:5]))), not skipped,
                    ((day * 7 + n) % 10) > 0, json.dumps(se, ensure_ascii=False),
                    max(1, 5 - g_cur // 5), max(1, min(10, g_cur // 2 + 1)),
                    max(1, 10 - g_cur // 3), chip)
                stats["diary"] += 1
                if chip and day < 5:  # НЯ из жалоб первой недели
                    draft = json.dumps({"source": "PWA_diary", "synthetic": True,
                                        "1071": "DRAFTED"}, ensure_ascii=False) \
                        if "ae1071" in flags else None
                    await conn.execute(
                        "INSERT INTO adverse_events_and_risks (patient_hash_id, meddra_term, "
                        "severity_ctcae, related_to_drug, transient_ae, roszdravnadzor_draft, "
                        "reported_at) VALUES ($1,$2,2,'POSSIBLE',true,$3::jsonb, "
                        "now() - make_interval(days => $4::int))",
                        P, SYM2MEDDRA[chip], draft, d_ago)
                    stats["ae"] += 1

            await conn.execute(
                "INSERT INTO patient_engagement (patient_hash_id, streak_days, best_streak, "
                "total_points, last_checkin_date, badges) VALUES ($1,$2,$3,$4, "
                "current_date - make_interval(days => $5::int), '[]'::jsonb)",
                P, streak, best, 10 * active_days, d - active_days + (1 if drop else 0))

            if "msg" in flags and n in MSG_QA:  # переписка пациент↔врач
                q, a = MSG_QA[n]
                await conn.execute(
                    "INSERT INTO messages (patient_hash_id, sender_role, thread_kind, priority, "
                    "body, created_at) VALUES ($1,'PATIENT','REGIMEN',1,$2, "
                    "now() - make_interval(days => 5::int))", P, q)
                await conn.execute(
                    "INSERT INTO messages (patient_hash_id, sender_role, sender_id, thread_kind, "
                    "priority, body, created_at) VALUES ($1,'DOCTOR',$2,'REGIMEN',1,$3, "
                    "now() - make_interval(days => 4::int))", P, doctor, a)
                stats["msgs"] += 2

            if "override-ckd" in flags:  # С-050: осознанный оверрайд жёсткого стопа
                await conn.execute(
                    "UPDATE therapy_plans_and_prescriptions SET override_reason = "
                    "'ХБП С4 (СКФ 22): назначение по решению ВК; доза 20 мг/сут, "
                    "креатинин 1 р/нед' WHERE patient_hash_id = $1", P)
                await conn.execute(
                    "INSERT INTO audit_log (actor_id, action, entity, entity_id, details) "
                    "VALUES ($1,'HARD_STOP_OVERRIDE','patient',$2,$3::jsonb)",
                    doctor, str(P),
                    json.dumps({"rule": "egfr_lt_60", "egfr": 22, "by": "врачебная комиссия",
                                "synthetic": True}, ensure_ascii=False))
                stats["overrides"] += 1

            if "ood" in flags:
                stats["ood_personas"] += 1
            stats["patients"] += 1

        await conn.execute(
            "INSERT INTO audit_log (actor_id, action, entity, entity_id, details) VALUES "
            "(NULL,'SEED_105','system','synthetic_personas',$1::jsonb)",
            json.dumps(stats, ensure_ascii=False))
        print("SEED-105 OK:", json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
