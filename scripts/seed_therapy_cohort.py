# -*- coding: utf-8 -*-
"""
scripts/seed_therapy_cohort.py — население стенда «реальными» пациентами на терапии.

Что делает (запуск на Debian, в контейнере api):
  1. Через ЖИВОЙ API: 5 приглашений + привязка (создаются PII-vault, ИДС,
     привязка к врачу — как в реальном потоке).
  2. Через SQL (asyncpg, от лица сервиса): заполняет ретро-историю по датам —
     шкалы GAD-7 с улучшением по неделям, дневник приёма (приверженность,
     пропуски), один «старый» подтверждённый критический алерт у patient 2.
  3. Печатает сводку по когорте.

Сценарии (по персонам с вебинара организаторов):
  A. Крылов-тип: 24 дня, GAD-7 16→7, приверженность 100%, водитель
  B. Савельева-тип: 16 дней, GAD-7 13→9, критический эпизод на дне 4 (подтверждён)
  C. Столярова-тип: 10 дней, GAD-7 11→9, сонливость (побочка), 1 пропуск
  D. Демьянов-тип: 8 дней, GAD-7 14→12, неровное настроение
  E. Веретенникова-тип: 5 дней, старт, GAD-7 16→15

Запуск: docker compose exec api python -m scripts.seed_therapy_cohort
"""
import asyncio
import base64
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/app")
BASE = "http://localhost:8000"
DSN = os.getenv("DATABASE_URL", "postgresql://compass_api:compass_api_demo@db:5432/compass")


def jwt_sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def api(path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(BASE + path,
                               data=json.dumps(data).encode() if data is not None else None,
                               headers=headers)
    return json.loads(urllib.request.urlopen(r, timeout=20).read())


COHORT = [
    # (fio_tag, days_of_therapy, gad_series [от старого к новому], skips, critical_day, mood_path)
    ("A-Крылов", 24, [16, 14, 12, 10, 9, 7], skips:=[], None, [2, 3, 3, 4, 4, 4]),
    ("B-Савельева", 16, [13, 12, 11, 10, 9], [3], 4, [2, 2, 3, 3]),
    ("C-Столярова", 10, [11, 10, 10, 9], [6], None, [3, 3, 3, 4]),
    ("D-Демьянов", 8, [14, 13, 13, 12], [5], None, [2, 2, 3, 3]),
    ("E-Веретенникова", 5, [16, 15], [], None, [2, 2]),
]


async def seed_sql(pool, patient_hash, days, gad, skips, critical_day, moods):
    from datetime import datetime, timedelta
    now = datetime.utcnow()

    async with pool.acquire() as conn:
        # GAD-7 ретро: каждый бальный отсчёт — раз в 4 дня назад от сегодня
        for i, score in enumerate(gad):
            days_ago = days - i * max(1, days // max(1, len(gad)))
            if days_ago < 0:
                days_ago = 0
            await conn.execute(
                "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, taken_at) "
                "VALUES ($1, 'GAD-7', $2, $3::jsonb, now() - ($4 || ' days')::interval)",
                patient_hash, score,
                json.dumps({"source": "PWA_checkin", "seed": True}), days_ago,
            )
        # PHQ-9 ретро (мягкое улучшение 12→6)
        for i, score in enumerate([12, 11, 9, 8, 7, 6][: max(2, len(gad) // 2)]):
            days_ago = days - i * max(1, days // max(1, len(gad)))
            await conn.execute(
                "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, taken_at) "
                "VALUES ($1, 'PHQ-9', $2, $3::jsonb, now() - ($4 || ' days')::interval)",
                patient_hash, score,
                json.dumps({"item9": 0, "source": "PWA_checkin", "seed": True}), days_ago,
            )
        # Приверженность: ежедневные записи, пропуски по сценарию
        for d in range(days):
            day = now - timedelta(days=d)
            skipped = d in skips
            await conn.execute(
                "INSERT INTO medication_adherence_logs (patient_hash_id, planned_at, taken_at, "
                "dose_skipped, before_meal_30min, event_uuid) "
                "VALUES ($1, $2, $3, $4, true, $5) ON CONFLICT (event_uuid) DO NOTHING",
                patient_hash, day, day, skipped,
                __import__("uuid").uuid4(),
            )
        # Критический эпизод (пациент B): алерт на день critical_day, уже подтверждён
        if critical_day is not None:
            moment = now - timedelta(days=critical_day)
            row = await conn.fetchrow(
                "INSERT INTO clinical_alerts_and_routing "
                "(patient_hash_id, category, critical, reasons, created_at, doctor_reaction_at) "
                "VALUES ($1, 'MANDATORY_PSYCHIATRIST', true, $2::jsonb, $3, $3) RETURNING alert_id",
                patient_hash, json.dumps(["C-SSRS = 2 (ретро-сценарий, подтверждён)"]), moment,
            )
            await conn.execute(
                "INSERT INTO patient_scale_assessments (patient_hash_id, scale_code, total_score, items, taken_at) "
                "VALUES ($1, 'C-SSRS', 2, $2::jsonb, $3)",
                patient_hash, json.dumps({"seed": True}), moment,
            )
            return row["alert_id"]
    return None


async def main() -> None:
    import asyncpg

    doc = api("/api/v1/auth/login", {"login": "neuro.ivlanov", "password": "compass-demo-2026"})
    H = doc["access_token"]
    print("[1/3] врач вошёл:", doc["principal_role"])

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    summary = []
    for tag, days, gad, skips, critical_day, moods in COHORT:
        inv = api("/api/v1/auth/invite", {}, H)
        pt = api("/api/v1/auth/bind", {"token": inv["token"]})
        ph = jwt_sub(pt["access_token"])
        alert_id = await seed_sql(pool, ph, days, gad, skips, critical_day, moods)
        summary.append(f"{tag}: {days} дн., GAD-7 {gad[0]}→{gad[-1]}, "
                       f"{'критический эпизод д.' + str(critical_day) if alert_id else 'без эпизодов'}")
        print("  +", summary[-1])

    await pool.close()
    print("[3/3] Когорта населена:")
    for s in summary:
        print("   •", s)
    print("Готово. Дашборд врача и таймлайны наполнены ретро-историей.")


if __name__ == "__main__":
    asyncio.run(main())
