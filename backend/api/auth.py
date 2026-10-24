# backend/api/auth.py — роутер аутентификации и привязки пациента.
#
# Пять эндпоинтов и одна идея: никаких третьих лиц между врачом и пациентом (Условия 1–2).
# Врач логинится паролем (argon2id), выдаёт пациенту одноразовый QR на 24 часа; пациент
# сканирует, привязывается к этому врачу, получает свой токен. ИДС-согласия создаются
# в момент привязки — без них пациентский контур не активируется (152-ФЗ ст. 10).
import base64
import io
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.services.auth import (  # noqa: E402
    BIND_TTL,
    DOCTOR_ROLES,
    AuthError,
    Principal,
    decode,
    mint_access,
    mint_bind,
    mint_refresh,
    needs_rehash,
    principal_from_access,
    rehash_password,
    sha256_hex,
    verify_password,
)
from backend.db.context import db_conn

# auto_error=False: свой 401 с WWW-Authenticate и понятным JSON, а не дефолт «Not authenticated»
_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------- anti-bruteforce
# Скользящее окно на IP: не более LOGIN_LIMIT попыток за LOGIN_WINDOW сек -> 429.
# В памяти процесса (для мультиворкера лимит действует на воркер — для демо достаточно;
# в бою заменить на общий Redis-счётчик, см. THREAT_MODEL).

import time as _time
from collections import defaultdict, deque

LOGIN_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "5"))
LOGIN_WINDOW = float(os.getenv("LOGIN_RATE_WINDOW", "60"))
_login_attempts: dict = defaultdict(deque)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_ok(ip: str) -> bool:
    if ip in ("127.0.0.1", "::1"):
        return True  # loopback — тесты и локальная разработка; лимит для внешних IP
    now = _time.monotonic()
    q = _login_attempts[ip]
    while q and now - q[0] > LOGIN_WINDOW:
        q.popleft()
    if len(q) >= LOGIN_LIMIT:
        return False
    q.append(now)
    return True


async def _audit(action: str, request: Request, actor_id=None,
                 entity: str = "users_auth", entity_id: str = "",
                 details: dict = None, pool=None) -> None:
    """Append-only запись в audit_log (роль приложения имеет только INSERT —
    прочитать журнал из-под API нельзя BY DESIGN, см. schema.sql)."""
    if pool is None:
        pool = request.app.state.pg_pool
    if pool is None:
        return
    try:
        async with db_conn(pool) as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor_id, action, entity, entity_id, details) "
                "VALUES ($1,$2,$3,$4,$5::jsonb)",
                actor_id, action, entity, entity_id,
                json.dumps(details or {}, ensure_ascii=False),
            )
    except Exception:  # noqa: BLE001 — аудит не должен ломать основной поток
        pass

auth_router = APIRouter(prefix="/api/v1/auth", tags=["Аутентификация и RBAC"])

# База пациентского PWA для QR-ссылки; на демо это локальный порт фронтенда пациента
# (python3 -m http.server 5501 -d frontend_patient). Токен приезжает как ?token=...
# и PWA читает его на любой странице — отдельный маршрут /bind не нужен.
PATIENT_APP_URL = os.getenv("PATIENT_APP_URL", "http://127.0.0.1:5501/")


# ------------------------------------------------------------------ зависимости
async def get_principal(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Principal:
    if creds is None:
        raise HTTPException(401, "Требуется Bearer-токен", headers={"WWW-Authenticate": "Bearer"})
    try:
        return principal_from_access(creds.credentials)
    except AuthError as e:
        raise HTTPException(e.status_code, e.detail, headers={"WWW-Authenticate": "Bearer"})


def require_roles(*roles: str):
    """Фабрика зависимостей: роутер объявляет Depends(require_roles("DOCTOR_NEUROLOGIST", ...))."""
    allowed = roles

    async def _guard(principal: Principal = Depends(get_principal)):
        if principal.role not in allowed:
            # 403, а не 401: токен валиден, роль не та — различие честно для аудита
            raise HTTPException(403, f"Роль {principal.role} не допущена к этому действию")
        return principal

    return _guard


require_doctor = require_roles(*DOCTOR_ROLES)


# ------------------------------------------------------------------ схемы
class LoginRequest(BaseModel):
    login: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class BindRequest(BaseModel):
    token: str


class InviteResponse(BaseModel):
    invite_url: str
    token: str
    qr_png_base64: str
    expires_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    principal_role: str
    principal_sub: str


# ------------------------------------------------------------------ эндпоинты
@auth_router.post("/login", response_model=TokenPair)
async def login(req: LoginRequest, request: Request):
    pool = request.app.state.pg_pool
    if pool is None:
        raise HTTPException(503, "БД недоступна")

    # anti-bruteforce: 5 попыток/мин на IP (Метрика ИБ; в бою — общий счётчик в Redis)
    if not _rate_ok(_client_ip(request)):
        await _audit("login_rate_limited", request, entity_id=req.login,
                     details={"ip": _client_ip(request)}, pool=pool)
        raise HTTPException(429, "Слишком много попыток входа — повторите через минуту")

    # Чтение users_auth допустимо роли приложения (см. комментарий в schema.sql):
    # пароль обязан верифицироваться на бэкенде, другого пути получить hash нет.
    async with db_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT user_id, role_code, password_hash, full_name FROM users_auth WHERE login = $1",
            req.login,
        )
    # Одинаковый 401 и на неизвестный логин, и на неверный пароль: не дарим переборщику
    # информацию о том, какие логины существуют.
    if row is None or not verify_password(row["password_hash"], req.password):
        # одинаковый ответ и на неизвестный логин, и на неверный пароль (анти-перебор),
        # но в аудит пишется честная причина — читать журнал может только владелец БД
        await _audit("login_fail", request, entity_id=req.login,
                     details={"ip": _client_ip(request), "known_login": row is not None}, pool=pool)
        raise HTTPException(401, "Неверный логин или пароль")

    await _audit("login_ok", request, actor_id=str(row["user_id"]),
                 entity_id=req.login, details={"ip": _client_ip(request), "role": row["role_code"]},
                 pool=pool)

    # Обновить параметры хеша, если дефолты argon2 изменились (канонический паттерн argon2-cffi)
    if needs_rehash(row["password_hash"]):
        async with db_conn(pool) as conn:
            await conn.execute(
                "UPDATE users_auth SET password_hash = $1 WHERE user_id = $2",
                rehash_password(req.password), row["user_id"],
            )

    return TokenPair(
        access_token=mint_access(str(row["user_id"]), row["role_code"]),
        refresh_token=mint_refresh(str(row["user_id"]), row["role_code"]),
        principal_role=row["role_code"],
        principal_sub=str(row["user_id"]),
    )


@auth_router.post("/refresh", response_model=TokenPair)
async def refresh(req: RefreshRequest):
    try:
        claims = decode(req.refresh_token, "refresh")
    except AuthError as e:
        raise HTTPException(e.status_code, e.detail)
    return TokenPair(
        access_token=mint_access(claims["sub"], claims["role"]),
        refresh_token=mint_refresh(claims["sub"], claims["role"]),
        principal_role=str(claims["role"]),
        principal_sub=str(claims["sub"]),
    )


@auth_router.get("/me")
async def me(principal: Principal = Depends(get_principal)):
    return {"role": principal.role, "sub": principal.sub,
            "is_doctor": principal.is_doctor, "is_patient": principal.is_patient}


@auth_router.post("/invite", response_model=InviteResponse, status_code=201)
async def create_invite(request: Request, principal: Principal = Depends(require_doctor)):
    """Врач генерирует одноразовое QR-приглашение (TTL 24 ч). В клинику уходит ТОЛЬКО
    ссылка вида /bind?token=...; клинического контента в ней нет и не будет."""
    pool = request.app.state.pg_pool
    token, jti = mint_bind(principal.sub)

    async with db_conn(pool) as conn:
        # Контекст здесь сервисный (роль пуста) — вставка в invites не под RLS.
        await conn.execute(
            "INSERT INTO patient_invite_tokens (jti, token_sha256, doctor_id, expires_at) "
            "VALUES ($1, $2, $3, now() + interval '24 hours')",
            uuid.UUID(jti), sha256_hex(token), uuid.UUID(principal.sub),
        )

    img = qrcode.make(f"{PATIENT_APP_URL}?token={token}", box_size=8)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return InviteResponse(
        invite_url=f"{PATIENT_APP_URL}?token={token}",
        token=token,
        qr_png_base64=base64.b64encode(buf.getvalue()).decode(),
        # истекает — ровно через BIND_TTL от создания; берём из константы, а не из
        # «недекодированного» токена: источник истины один
        expires_at=datetime.now(timezone.utc) + BIND_TTL,
    )


@auth_router.post("/bind", response_model=TokenPair, status_code=201)
async def bind(req: BindRequest, request: Request):
    """Пациент сканирует QR -> здесь. Что происходит по шагам:
    1) проверяем подпись/срок bind-токена;
    2) атомарно помечаем приглашение использованным (двойное использование невозможно
       по построению: UPDATE ... WHERE used_at IS NULL);
    3) создаём пациента в PII-вальте через SECURITY DEFINER-функцию (роль приложения
       не умеет читать вальт — только births через неё);
    4) привязка к врачу + два ИДС-согласия (контекст AUTH_SERVICE);
    5) выдаём пациентский токен (sub = patient_hash_id).
    """
    pool = request.app.state.pg_pool
    try:
        claims = decode(req.token, "bind")
    except AuthError as e:
        raise HTTPException(401, f"Приглашение недействительно: {e.detail}")

    doctor_id = claims["sub"]
    jti = claims["jti"]

    async with db_conn(pool, role="AUTH_SERVICE") as conn:
        consumed = await conn.fetchrow(
            "UPDATE patient_invite_tokens SET used_at = now() "
            "WHERE jti = $1 AND used_at IS NULL AND expires_at > now() "
            "RETURNING doctor_id",
            uuid.UUID(jti),
        )
        if consumed is None:
            raise HTTPException(409, "Приглашение уже использовано или истекло")

        patient_hash = await conn.fetchval("SELECT create_patient_vault_entry()")
        # Пациент родился только что — теперь можно дозаписать его в приглашение
        # (само потребление уже атомарно зафиксировано выше через used_at).
        await conn.execute(
            "UPDATE patient_invite_tokens SET used_by_patient = $1 WHERE jti = $2",
            patient_hash, uuid.UUID(jti),
        )
        await conn.execute(
            "INSERT INTO doctor_patient_bindings (doctor_id, patient_hash_id, consents) "
            "VALUES ($1, $2, $3::jsonb)",
            uuid.UUID(doctor_id), patient_hash,
            '[{"type":"ИДС_телемониторинг","signed_at":"QR-bind","method":"QR"},'
            '{"type":"ИДС_обработка_ПДн","signed_at":"QR-bind","method":"QR"}]',
        )
        await conn.execute(
            "INSERT INTO consents (patient_hash_id, consent_type, text_version) "
            "VALUES ($1,'TELEMONITORING','v2026-10-16.1'), ($1,'PERSONAL_DATA_PROCESSING','v2026-10-16.1')",
            patient_hash,
        )
        # Пустая запись вовлечённости: серия/скоринг посчитаются с первого чек-ина
        await conn.execute(
            "INSERT INTO patient_engagement (patient_hash_id) VALUES ($1)", patient_hash
        )

    return TokenPair(
        access_token=mint_access(str(patient_hash), "PATIENT"),
        refresh_token=mint_refresh(str(patient_hash), "PATIENT"),
        principal_role="PATIENT",
        principal_sub=str(patient_hash),
    )


@auth_router.get("/my-patients",
                 summary="[Врач] Когорта терапии: пациенты на ведении + последняя динамика")
async def my_patients(request: Request, doctor: Principal = Depends(require_doctor)):
    """Кого врач ведёт сейчас: привязка активна + последняя GAD-7, PHQ-9,
    приверженность и «серия дней» из чек-инов. RLS отрезает чужих пациентов
    (app_is_attending), данные — только в контуре МО."""
    pool = request.app.state.pg_pool
    if pool is None:
        raise HTTPException(503, "БД недоступна")
    async with db_conn(pool, role=doctor.role, user_id=doctor.sub) as conn:
        rows = await conn.fetch(
            """
            SELECT b.patient_hash_id::text AS patient_hash,
                   (SELECT total_score FROM patient_scale_assessments s
                     WHERE s.patient_hash_id = b.patient_hash_id AND s.scale_code = 'GAD-7'
                     ORDER BY taken_at DESC LIMIT 1) AS gad7_last,
                   (SELECT total_score FROM patient_scale_assessments s
                     WHERE s.patient_hash_id = b.patient_hash_id AND s.scale_code = 'GAD-7'
                     ORDER BY taken_at ASC LIMIT 1) AS gad7_first,
                   (SELECT count(*) FROM medication_adherence_logs m
                     WHERE m.patient_hash_id = b.patient_hash_id
                       AND m.dose_skipped) AS doses_skipped,
                   (SELECT count(*) FROM medication_adherence_logs m
                     WHERE m.patient_hash_id = b.patient_hash_id) AS doses_logged,
                   b.bound_at::date AS bound_since
            -- Zero-PII: JOIN к patient_pii_vault невозможен и не нужен — роль
            -- приложения не имеет гранта на vault BY DESIGN; врач видит ФИО в ЭМК.
            FROM doctor_patient_bindings b
            WHERE b.doctor_id = $1 AND b.active
            ORDER BY b.bound_at DESC
            """,
            uuid.UUID(doctor.sub),
        )
    return {"count": len(rows), "patients": [dict(r) for r in rows]}
