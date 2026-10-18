#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed.py — демо-данные COMPASS-HYBRID v2.6 (Метрика №12 + живые графики в UI).
Запуск: python3 backend/db/seed.py
DSN владельца (суперпользователь кластера — обходит RLS для заливки):
  OWNER_DATABASE_URL=postgresql://compass_app:compass_demo@localhost:5433/compass
"""
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/
from bio_paths import KB_PATH  # noqa: E402

OWNER_DSN = os.getenv(
    "OWNER_DATABASE_URL",
    "postgresql://compass_app:compass_demo@localhost:5433/compass",
)

try:
    from argon2 import PasswordHasher

    ph = PasswordHasher()

    def hash_pw(pw: str) -> str:
        return ph.hash(pw)
except ImportError:  # фоллбэк для быстрого демо
    import hashlib

    def hash_pw(pw: str) -> str:
        return "sha256$" + hashlib.sha256(pw.encode()).hexdigest()


async def wait_for_schema(dsn: str, timeout_s: float = 90.0) -> None:
    """Ждём, пока initdb догонит схему до конца.

    Ловушка, на которую мы напоролись: pg_isready (и docker healthcheck) зелёные ещё
    на временном сервере initdb, когда из схемы создана только половина таблиц.
    Маркер готовности — rbac_roles с 6 ролями: этот INSERT стоит в самом конце schema.sql.
    """
    import time as _time

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn)
            try:
                n = await conn.fetchval("SELECT count(*) FROM rbac_roles")
                if n and int(n) >= 6:
                    return
            finally:
                await conn.close()
        except Exception:  # noqa: BLE001 — база поднимается, это нормально
            pass
        await asyncio.sleep(1.5)
    raise RuntimeError("Схема не применилась за 90 с — смотри docker logs compass_db")


async def main() -> None:
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))

    await wait_for_schema(OWNER_DSN)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            """
            TRUNCATE critical_events_outbox, notification_deliveries, message_receipts, messages,
                     consents, patient_engagement, rule_versions, scale_versions, guideline_sources,
                     clinical_alerts_and_routing, adverse_events_and_risks,
                     medication_adherence_logs, therapy_plans_and_prescriptions,
                     multimodal_biomarkers_ts, patient_scale_assessments, ehr_encounters_parsed,
                     doctor_patient_bindings, patient_pii_vault, users_auth,
                     psychometric_scales_registry, grls_drugs_ohpl_catalog
            RESTART IDENTITY CASCADE;
            """
        )

        # Пользователи (пароль у всех 'demo' — argon2id)
        dr_neuro = await conn.fetchval(
            "INSERT INTO users_auth (role_code, login, password_hash, full_name) VALUES ('DOCTOR_NEUROLOGIST','neuro.ivlanov',$1,'Иванов Алексей Викторович (невролог)') RETURNING user_id",
            hash_pw("compass-demo-2026"),
        )
        dr_psych = await conn.fetchval(
            "INSERT INTO users_auth (role_code, login, password_hash, full_name, mfa_enabled) VALUES ('DOCTOR_PSYCHIATRIST','psych.petrova',$1,'Петрова Мария Сергеевна (психиатр)', true) RETURNING user_id",
            hash_pw("compass-demo-2026"),
        )
        await conn.fetchval(
            "INSERT INTO users_auth (role_code, login, password_hash, full_name, mfa_enabled) VALUES ('ADMIN_METHODOLOGIST','admin.metod',$1,'Методист Клинического Компаса', true)",
            hash_pw("compass-demo-2026"),
        )

        patients = []
        for i, (sex, birth) in enumerate([(1, date(1989, 3, 14)), (0, date(1958, 11, 2)), (0, date(2001, 6, 27))], start=1):
            pid = await conn.fetchval(
                "INSERT INTO patient_pii_vault (fio_encrypted, phone_encrypted, snils_encrypted, birth_date, sex, key_version) VALUES ($1,$2,$3,$4,$5,0) RETURNING patient_hash_id",
                b"DEMO-FIO-ENC-" + bytes([i]) * 8,
                b"DEMO-PHONE-ENC-" + bytes([i]) * 8,
                b"DEMO-SNILS-ENC-" + bytes([i]) * 8,
                birth, sex,
            )
            patients.append(pid)

        # Привязки врач-пациент + ИДС
        binds = [(dr_neuro, patients[0]), (dr_neuro, patients[1]), (dr_psych, patients[2])]
        for doc, pat in binds:
            await conn.execute(
                "INSERT INTO doctor_patient_bindings (doctor_id, patient_hash_id, consents) VALUES ($1,$2,$3)",
                doc, pat,
                json.dumps([{"type": "ИДС_телемониторинг", "signed_at": "2026-10-16T09:30:00+03", "method": "QR"}]),
            )

        # Реестр шкал + каталог ГРЛС/ОХЛП из базы знаний биоконтура
        for code, manifest in kb["psychometric_scales_registry"].items():
            await conn.execute(
                "INSERT INTO psychometric_scales_registry (scale_code, manifest) VALUES ($1,$2)",
                code, json.dumps(manifest, ensure_ascii=False),
            )
        for code, drug in kb["therapy_options_grls_catalog"].items():
            manifest = dict(drug)
            manifest["reference_corridors"] = kb["aviandr_clinical_trials_reference_corridors"]
            await conn.execute(
                "INSERT INTO grls_drugs_ohpl_catalog (drug_code, manifest) VALUES ($1,$2)",
                code, json.dumps(manifest, ensure_ascii=False),
            )

        # Слепок ЭМК (пациент 1 — пост-COVID тревога, целевой сценарий №1)
        await conn.execute(
            "INSERT INTO ehr_encounters_parsed (patient_hash_id, doctor_id, raw_text_hash, parsed, icd10_primary) VALUES ($1,$2,$3,$4,'F43.2')",
            patients[0], dr_neuro, "demo-hash-f432",
            json.dumps({
                "жалобы": ["тревога", "инсомния", "сердцебиение"],
                "анамнез": "COVID-19 1.5 мес назад, стрессовая реакция",
                "лабораторные": {"creatinine_umol_l": 86, "egfr_ckd_epi": 103.4, "alt": 22, "ast": 20},
                "cyp2d6_phenotype": "NM",
                "профессия": "водит автомобиль",
            }, ensure_ascii=False),
        )

        # Оценки по шкалам: GAD-7 12 -> 4 (ремиссия к 4-й неделе, сценарий №1)
        for score, delta, day in [(12, None, 0), (8, -4, 14), (4, -4, 28)]:
            await conn.execute(
                "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, delta_prev, taken_at) VALUES ($1,'GAD-7',$2,$3,$4, now() - ($5 || ' days')::interval)",
                patients[0], score, json.dumps({"source": "seed"}), delta, str(28 - day),
            )

        # Биомаркеры: голос DAM + rPPG
        await conn.execute(
            "INSERT INTO multimodal_biomarkers_ts (patient_hash_id, source, values) VALUES ($1,'VOICE_DAM',$2), ($1,'RPPG',$3)",
            patients[0],
            json.dumps({"anx_class": 1, "dep_class": 0, "jitter_pct": 0.9, "pause_ratio": 0.17}),
            json.dumps({"hrv_rmssd_ms": 41.2, "baevsky_stress_index": 128.0}),
        )

        # План терапии (In-Label) + дневник приёма
        plan_id = await conn.fetchval(
            "INSERT INTO therapy_plans_and_prescriptions (patient_hash_id, doctor_id, drug_code, label_status, dosing, confirmed_by_doctor_id) VALUES ($1,$2,'AVIANDR','IN_LABEL',$3,$2) RETURNING plan_id",
            patients[0], dr_neuro, json.dumps({"scheme": "20 мг 2 р/сут", "before_meal_min": 30}),
        )
        for k in range(14):
            await conn.execute(
                "INSERT INTO medication_adherence_logs (patient_hash_id, plan_id, planned_at, taken_at, before_meal_30min, pdc_pct, kop25_score) VALUES ($1,$2, now() - ($3 || ' days')::interval + interval '08:00', now() - ($3 || ' days')::interval + interval '07:52', true, 100, 82)",
                patients[0], plan_id, str(k),
            )

        # НЯ первых дней (транзиторная сонливость) + алерты
        await conn.execute(
            "INSERT INTO adverse_events_and_risks (patient_hash_id, meddra_term, severity_ctcae, related_to_drug, transient_ae, roszdravnadzor_draft) VALUES ($1,'Сонливость',1,'POSSIBLE',true,$2)",
            patients[0], json.dumps({"status": "НЕ_ТРЕБУЕТ_ИЗВЕЩЕНИЯ_1071", "причина": "нежелательное явление 1-10%, транзиторное"}),
        )
        await conn.execute(
            "INSERT INTO clinical_alerts_and_routing (patient_hash_id, category, critical, reasons, created_at, delivered_to_doctor_at, delivery_ms, action_taken) VALUES ($1,'PRIMARY_CARE_MANAGEMENT',false,$2, now() - interval '28 days', now() - interval '28 days', 41, 'Терапия подтверждена врачом')",
            patients[0], json.dumps(["Пост-COVID расстройство адаптации F43.2 — In-Label"]),
        )
        await conn.execute(
            "INSERT INTO critical_events_outbox (event_uuid, payload, published_to_redis) VALUES (gen_random_uuid(), $1, true)",
            json.dumps({"patient": 0, "type": "CHECKIN", "cssrs": 0, "demo": True}),
        )

        # ИДС пациента 0 (без согласия пациентский контур не активируется)
        await conn.execute(
            "INSERT INTO consents (patient_hash_id, consent_type, text_version) VALUES ($1,'TELEMONITORING','v2026-10-16.1'), ($1,'PERSONAL_DATA_PROCESSING','v2026-10-16.1')",
            patients[0],
        )

        # Источники обоснований (метрика 5): сверены по первоисточникам, дата сверки честная.
        # Остальные строки (КР, шкалы) добавит Михаил после сверки — см. docs/VERIFICATION.md.
        await conn.execute(
            "INSERT INTO guideline_sources (source_id, doc_title, doc_ref, verified_url, verified_at, note) VALUES "
            "('AVIANDR_OHLP','Инструкция по применению Авиандр® табл. 20 мг (маритупирдин)','РУ ЛС-..., ГРЛС, регистрация 31.05.2023 (Авинейро/ХимРар)',"
            "'https://aptekamos.ru/tovary/lekarstva/maritupirdin-16805/aviandr-tabletki-20mg-108995/instrukciya', '2026-10-18',"
            "'8 противопоказаний и режим дозирования сверены дословно; номер РУ уточнить в ГРЛС'), "
            "('KINTSUGI_DAM','KintsugiHealth/dam model card (Whisper-Small EN, Apache-2.0)','Hugging Face',"
            "'https://huggingface.co/KintsugiHealth/dam', '2026-10-18', 'English-only: вне клинического контура рекомендаций')"
        )

        # Вовлечённость пациента 0: серия 14 дней, скоринг как задумано в PRODUCT_DESIGN
        await conn.execute(
            "INSERT INTO patient_engagement (patient_hash_id, streak_days, best_streak, daily_score, total_points, badges) VALUES ($1, 14, 14, 90, 1260, $2)",
            patients[0], json.dumps(["14_days_adherence", "first_week_complete"]),
        )

        # Сообщение из чата (задача 6): быстрый ответ врача из утверждённых шаблонов
        msg_id = await conn.fetchval(
            "INSERT INTO messages (patient_hash_id, sender_role, sender_id, thread_kind, priority, body) VALUES ($1,'DOCTOR',$2,'REGIMEN',1,$3) RETURNING message_id",
            patients[0], dr_neuro,
            "Лёгкая сонливость в первые дни — ожидаемая адаптация, обычно проходит самостоятельно (инструкция Авиандр®). "
            "Если усиливается — напишите здесь. Решение о продолжении терапии принимает только ваш врач.",
        )
        await conn.execute("INSERT INTO message_receipts (message_id, delivered_at) VALUES ($1, now())", msg_id)

        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM users_auth) u, (SELECT count(*) FROM patient_pii_vault) p,
                   (SELECT count(*) FROM psychometric_scales_registry) s, (SELECT count(*) FROM grls_drugs_ohpl_catalog) d,
                   (SELECT count(*) FROM medication_adherence_logs) a, (SELECT count(*) FROM clinical_alerts_and_routing) al
            """
        )
        print(f"[OK] Seed выполнен: users={counts['u']}, patients={counts['p']}, scales={counts['s']}, drugs={counts['d']}, adherence={counts['a']}, alerts={counts['al']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
