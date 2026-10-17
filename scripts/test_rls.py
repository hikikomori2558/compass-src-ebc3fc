#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_rls.py — проверка Row-Level Security (Метрика №12 + ст. 13 323-ФЗ).

Идея теста простая: каждый «пользователь» получает своё соединение — ровно как в бою,
где контекст выставляет middleware бэкенда. Врач А не должен видеть пациентов врача Б,
пациент — только себя, а роли приложения вообще нельзя заглядывать в patient_pii_vault.
Проверки честные: считаем строки, а не верим политикам на слово.

Запуск: python3 scripts/test_rls.py (сначала docker compose up -d db и backend/db/seed.py)
APP_DATABASE_URL / OWNER_DATABASE_URL можно переопределить через окружение.
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

APP_DSN = os.getenv(
    "APP_DATABASE_URL",
    "postgresql://compass_api:compass_api_demo@localhost:5432/compass",
)
OWNER_DSN = os.getenv(
    "OWNER_DATABASE_URL",
    "postgresql://compass_app:compass_demo@localhost:5432/compass",
)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


async def session_counts(dsn, role, user_id=None):
    """Отдельное соединение с выставленным контекстом app.role/app.user_id."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.role', $1, false)", role)
        if user_id:
            await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id)
        return await conn.fetchval("SELECT count(*) FROM ehr_encounters_parsed")
    finally:
        await conn.close()


async def main() -> None:
    owner = await asyncpg.connect(OWNER_DSN)
    users = await owner.fetch(
        "SELECT user_id, login, role_code FROM users_auth WHERE login IN ('neuro.ivlanov','psych.petrova') ORDER BY login"
    )
    await owner.close()
    assert len(users) == 2, "seed не выполнен: нет демо-врачей (запусти python3 backend/db/seed.py)"
    doc_a_id, doc_b_id = str(users[0]["user_id"]), str(users[1]["user_id"])

    n_owner = await session_counts(APP_DSN, "ADMIN_METHODOLOGIST", doc_a_id)
    n_a = await session_counts(APP_DSN, "DOCTOR_NEUROLOGIST", doc_a_id)
    n_b = await session_counts(APP_DSN, "DOCTOR_PSYCHIATRIST", doc_b_id)

    # В seed ровно один слепок ЭМК, и он принадлежит неврологу — поэтому ассерты точные,
    # а не «больше нуля». Размазанные проверки — способ спрятать баг, не способ поймать.
    check("Admin видит все слепки ЭМК", n_owner == 1, f"rows={n_owner}")
    check(f"Врач {users[0]['login']} видит ровно своих", n_a == 1, f"rows={n_a}")
    check("Изоляция: врач Б (психиатр) не видит чужих слепков", n_b == 0, f"rows={n_b}")

    # Fail-closed: без контекста — пусто
    conn = await asyncpg.connect(APP_DSN)
    try:
        n_noctx = await conn.fetchval("SELECT count(*) FROM ehr_encounters_parsed")
        check("Fail-closed: без app.role данных нет", n_noctx == 0, f"rows={n_noctx}")

        # Пациент видит только себя. Берём НЕ случайного пациента, а того, у кого
        # точно есть слепок ЭМК (иначе тест мерил случайность, а не политику),
        # и тут же проверяем негатив: любой другой пациент видит ноль строк.
        await conn.close()
        conn = await asyncpg.connect(OWNER_DSN)
        pat_with = await conn.fetchval("SELECT patient_hash_id FROM ehr_encounters_parsed LIMIT 1")
        pat_rows = await conn.fetch(
            "SELECT patient_hash_id FROM patient_pii_vault WHERE patient_hash_id <> $1 LIMIT 1", pat_with
        )
        pat_without = pat_rows[0]["patient_hash_id"]
        await conn.close()
        conn = await asyncpg.connect(APP_DSN)
        await conn.execute("SELECT set_config('app.role', 'PATIENT', false)")
        await conn.execute("SELECT set_config('app.user_id', $1, false)", str(pat_with))
        n_pat = await conn.fetchval("SELECT count(*) FROM ehr_encounters_parsed")
        check("Пациент видит ровно свою ЭМК", n_pat == 1, f"rows={n_pat}")
        await conn.execute("SELECT set_config('app.user_id', $1, false)", str(pat_without))
        n_pat2 = await conn.fetchval("SELECT count(*) FROM ehr_encounters_parsed")
        check("Чужой пациент не видит ничего", n_pat2 == 0, f"rows={n_pat2}")

        # Zero-PII: vault недоступен роли приложения
        try:
            await conn.fetchval("SELECT count(*) FROM patient_pii_vault")
            check("Zero-PII: patient_pii_vault запрещён роли приложения", False, "SELECT прошёл!")
        except asyncpg.PostgresError as e:
            check("Zero-PII: patient_pii_vault запрещён роли приложения", True, type(e).__name__)

        # RLS на алертах: врач без пациентов не видит ничего
        await conn.execute("SELECT set_config('app.role', 'DOCTOR_PSYCHIATRIST', false)")
        await conn.execute("SELECT set_config('app.user_id', $1, false)", doc_b_id)
        n_alerts_b = await conn.fetchval("SELECT count(*) FROM clinical_alerts_and_routing")
        check("Алерты: врач Б не видит чужих", n_alerts_b == 0, f"rows={n_alerts_b}")
    finally:
        await conn.close()

    print()
    if FAILURES:
        print(f"[FAIL] RLS-тест провален: {FAILURES}")
        sys.exit(1)
    print("[OK] Все проверки RLS пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
