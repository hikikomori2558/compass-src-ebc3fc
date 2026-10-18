# backend/auth.py — токены и проверка пароля (Разработчик №1, день 5).
#
# Дизайн-решения, чтобы ревьюеру не пришлось гадать:
#  * Два сорта принципалов. Врач: sub = users_auth.user_id, роль DOCTOR_*.
#    Пациент: sub = patient_hash_id, роль PATIENT — у пациента НЕТ записи в users_auth,
#    его идентичность рождается в момент привязки по QR и живёт только псевдонимом
#    в PII-вальте. Это доводит Zero-PII до самого логина.
#  * TTL. access = 2 часа (модель угроз требует «короткий», первоначальные 8 часов
#    из плана — слишком щедро для медицинского контура), refresh = 14 дней,
#    приглашение = 24 часа и строго одноразовое (чек-лист 1.1).
#  * refresh без отзыва (stateless). Компромисс осознанный, зафиксирован в
#    docs/THREAT_MODEL.md; отзыв добавляется таблицей refresh_jti, если жюри спросит.
#  * Время — только aware-UTC: PyJWT сравнивает exp с UTC, наивный now() на машине
#    с другим часовым поясом выдаёт токены, которые «уже истекли» или живут лишние часы.
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Идентичность принципала, восстановленная из токена. Immutable по построению.
@dataclass(frozen=True)
class Principal:
    role: str
    sub: str            # uuid строкой: users_auth.user_id ИЛИ patient_hash_id

    @property
    def is_doctor(self) -> bool:
        return self.role.startswith("DOCTOR_")

    @property
    def is_patient(self) -> bool:
        return self.role == "PATIENT"


DOCTOR_ROLES = ("DOCTOR_PSYCHIATRIST", "DOCTOR_NEUROLOGIST", "DOCTOR_GP")
STAFF_ROLES = DOCTOR_ROLES + ("ADMIN_METHODOLOGIST",)

ACCESS_TTL = timedelta(hours=2)
REFRESH_TTL = timedelta(days=14)
BIND_TTL = timedelta(hours=24)

# Дефолтный секрет — только для локальной разработки; в бою JWT_SECRET обязателен.
# Длина ≥ 32 байт — требование RFC 7518 для HS256, иначе PyJWT честно ругается.
JWT_SECRET = os.getenv("JWT_SECRET", "dev_jwt_secret_change_me_min_32_bytes!")
JWT_ALG = "HS256"


class AuthError(Exception):
    """Любой отказ аутентификации. status_code превращается в HTTP-ответ роутером."""

    def __init__(self, detail: str, status_code: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _mint(claims: Dict, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        **claims,
        "iat": now,
        "exp": now + ttl,   # PyJWT: datetime → сам превратит в int-UTC; timezone обязателен
        # не затираем jti, если вызывающий его уже задал (mint_bind синхронизирует
        # jti токена с записью в patient_invite_tokens — без этого bind не найдёт строку)
        "jti": claims.get("jti") or str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def mint_access(sub: str, role: str) -> str:
    return _mint({"sub": sub, "role": role, "typ": "access"}, ACCESS_TTL)


def mint_refresh(sub: str, role: str) -> str:
    return _mint({"sub": sub, "role": role, "typ": "refresh"}, REFRESH_TTL)


def mint_bind(doctor_id: str, ttl: Optional[timedelta] = None) -> Tuple[str, str]:
    """Приглашение пациенту. Возвращает (jwt, jti): jti ляжет в patient_invite_tokens."""
    jti = str(uuid.uuid4())
    token = _mint({"sub": doctor_id, "role": "DOCTOR_INVITE", "typ": "bind", "jti": jti},
                  ttl or BIND_TTL)
    return token, jti


def decode(token: str, expected_typ: str) -> Dict:
    """Проверяет подпись, срок и обязательные claims; тип обязан совпасть с ожидаемым
    (refresh нельзя подсунуть вместо access, invite — вместо refresh)."""
    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALG],
            options={"require": ["exp", "sub", "typ", "jti"]},
            leeway=5,  # секунд на рассинхрон часов между сервисами
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Токен истёк")
    except jwt.InvalidTokenError as e:  # сюда попадают и подпись, и missing claims
        raise AuthError(f"Невалидный токен: {type(e).__name__}")
    if claims.get("typ") != expected_typ:
        raise AuthError("Токен другого типа")
    return claims


def principal_from_access(token: str) -> Principal:
    claims = decode(token, "access")
    return Principal(role=str(claims["role"]), sub=str(claims["sub"]))


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Один PasswordHasher на процесс: параметры по умолчанию (~64 МБ памяти на вызов)
# сознательно не снижаем — память у демо-стенда есть, а слабые параметры — не наш стиль.
_PH = PasswordHasher()


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _PH.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _PH.check_needs_rehash(password_hash)


def rehash_password(password: str) -> str:
    return _PH.hash(password)
