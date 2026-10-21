#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_auth.py — жизненный цикл аутентификации и привязки (день 5, Условия 1–2).

Прогон: python3 benchmarks/test_auth.py (нужны docker compose up -d db + seed).
Здесь проверяется не «не падает», а каждое обещание из документации:
  * пароль неверный и логин неизвестный — одинаковый 401 (не раскрываем перебор);
  * access вместо refresh и наоборот — 401 «токен другого типа»;
  * приглашение одноразовое (атомарно), повторное использование — 409;
  * пациент получает роль PATIENT, врач-эндпоинт для него — 403;
  * в БД появляются: пациент в вальте, привязка к правильному врачу, 2 ИДС, engagement;
  * RLS-изоляция: новый пациент видит только себя, чужих ЭМК — ноль.
"""
import asyncio
import atexit
import base64
import sys
import uuid
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import asyncpg
import jwt as pyjwt
from fastapi.testclient import TestClient

import backend.services.auth as auth_mod
from backend.main import app

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# TestClient обязан работать как контекст-менеджер — иначе lifespan (и пул БД)
# не поднимется. ExitStack держит клиент живым до конца скрипта.
_stack = ExitStack()
c = _stack.enter_context(TestClient(app))
atexit.register(_stack.close)

# --- 1. Логин ---
r = c.post("/api/v1/auth/login", json={"login": "neuro.ivlanov", "password": "compass-demo-2026"})
check("Логин невролога: 200 + пара токенов", r.status_code == 200 and r.json()["refresh_token"])
doc_tokens = r.json()
check("Роль в ответе — DOCTOR_NEUROLOGIST", doc_tokens["principal_role"] == "DOCTOR_NEUROLOGIST")

r = c.post("/api/v1/auth/login", json={"login": "neuro.ivlanov", "password": "wrong-password-123"})
check("Неверный пароль -> 401", r.status_code == 401)
r = c.post("/api/v1/auth/login", json={"login": "nosuchuser", "password": "whatever123"})
check("Неизвестный логин -> 401, текст тот же (не раскрываем существование)",
      r.status_code == 401 and r.json()["detail"] == "Неверный логин или пароль")

HDR = {"Authorization": f"Bearer {doc_tokens['access_token']}"}
r = c.get("/api/v1/auth/me", headers=HDR)
check("/me: врач опознан", r.status_code == 200 and r.json()["is_doctor"] is True)

# --- 2. Типы токенов не взаимозаменяемы ---
r = c.post("/api/v1/auth/refresh", json={"refresh_token": doc_tokens["access_token"]})
check("access вместо refresh -> 401 «другого типа»", r.status_code == 401 and "типа" in r.json()["detail"])
r = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {doc_tokens['refresh_token']}"})
check("refresh вместо access -> 401", r.status_code == 401)
r = c.get("/api/v1/auth/me")
check("Без токена -> 401 c WWW-Authenticate",
      r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer")
forged = pyjwt.encode({"sub": "x", "role": "ADMIN_METHODOLOGIST", "typ": "access",
                       "exp": 9999999999, "jti": "x"}, "evil-secret-32-bytes-minimum-length!", algorithm="HS256")
r = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
check("Токен с чужой подписью -> 401", r.status_code == 401)

# --- 3. Refresh ротация ---
r = c.post("/api/v1/auth/refresh", json={"refresh_token": doc_tokens["refresh_token"]})
check("Refresh -> новая пара", r.status_code == 200 and r.json()["access_token"] != doc_tokens["access_token"])

# --- 4. Приглашение (QR) ---
r = c.post("/api/v1/auth/invite", headers=HDR)
check("Приглашение: 201 + QR", r.status_code == 201)
inv = r.json()
png = base64.b64decode(inv["qr_png_base64"])
check("QR — настоящий PNG", png[:8] == b"\x89PNG\r\n\x1a\n", f"{len(png)} байт")
check("Ссылка ведёт на PWA привязки, токен внутри", inv["invite_url"].startswith("http") and "token=" in inv["invite_url"])

# Пациент не может создавать приглашения (нужен врач) — проверим после bind.
r = c.post("/api/v1/auth/invite")
check("Приглашение без токена -> 401", r.status_code == 401)

# --- 5. Привязка пациента ---
r = c.post("/api/v1/auth/bind", json={"token": inv["token"]})
check("Bind: 201 + пациентский токен", r.status_code == 201)
pat = r.json()
check("Роль пациента — PATIENT, sub — uuid вальта",
      pat["principal_role"] == "PATIENT" and len(uuid.UUID(pat["principal_sub"]).hex) == 32)

# Повторное использование того же приглашения — 409, а не второй пациент
r = c.post("/api/v1/auth/bind", json={"token": inv["token"]})
check("Повторный bind того же QR -> 409", r.status_code == 409)

# Поддельное приглашение
r = c.post("/api/v1/auth/bind", json={"token": "garbage.token.here"})
check("Мусорный токен -> 401", r.status_code == 401)
expired_token, _ = auth_mod.mint_bind(str(uuid.uuid4()), ttl=timedelta(seconds=-10))
r = c.post("/api/v1/auth/bind", json={"token": expired_token})
check("Истёкшее приглашение -> 401", r.status_code == 401)

# --- 6. RBAC: пациент не врач ---
PHDR = {"Authorization": f"Bearer {pat['access_token']}"}
r = c.post("/api/v1/auth/invite", headers=PHDR)
check("Пациент не может звать пациентов -> 403", r.status_code == 403)
r = c.get("/api/v1/auth/me", headers=PHDR)
check("/me: пациент опознан", r.status_code == 200 and r.json()["is_patient"] is True)


# --- 7. Состояние БД и RLS-изоляция (напрямую, через owner-соединение) ---
async def db_checks():
    conn = await asyncpg.connect("postgresql://compass_app:compass_demo@localhost:5433/compass")
    try:
        invite = await conn.fetchrow(
            "SELECT doctor_id, used_at, used_by_patient FROM patient_invite_tokens ORDER BY created_at DESC LIMIT 1")
        check("Приглашение помечено использованным", invite["used_at"] is not None)
        check("Привязан правильный врач (создатель QR)",
              str(invite["doctor_id"]) == doc_tokens["principal_sub"])
        check("used_by_patient = новый пациент", str(invite["used_by_patient"]) == pat["principal_sub"])

        binds = await conn.fetchval(
            "SELECT count(*) FROM doctor_patient_bindings WHERE patient_hash_id = $1", invite["used_by_patient"])
        cons = await conn.fetchval(
            "SELECT count(*) FROM consents WHERE patient_hash_id = $1 AND revoked_at IS NULL", invite["used_by_patient"])
        eng = await conn.fetchval(
            "SELECT count(*) FROM patient_engagement WHERE patient_hash_id = $1", invite["used_by_patient"])
        check("Привязка создана", binds == 1)
        check("Два ИДС созданы", cons == 2)
        check("Строка вовлечённости создана", eng == 1)
    finally:
        await conn.close()

    # RLS глазами роли приложения: новый пациент видит только себя
    conn = await asyncpg.connect("postgresql://compass_api:compass_api_demo@localhost:5433/compass")
    try:
        await conn.execute("SELECT set_config('app.role', 'PATIENT', false)")
        await conn.execute("SELECT set_config('app.user_id', $1, false)", pat["principal_sub"])
        n_ehr = await conn.fetchval("SELECT count(*) FROM ehr_encounters_parsed")
        n_bind = await conn.fetchval("SELECT count(*) FROM doctor_patient_bindings")
        check("RLS: у нового пациента 0 чужих ЭМК", n_ehr == 0)
        check("RLS: видит ровно свою привязку", n_bind == 1)
    finally:
        await conn.close()


asyncio.run(db_checks())

print()
if FAILURES:
    print(f"[FAIL] Провалено {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("[OK] Все проверки аутентификации пройдены.")
