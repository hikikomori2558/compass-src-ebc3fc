# backend — Python-контур COMPASS-HYBRID v2.6.
# Слои (зависимости идут только вниз: api -> services -> domain -> bridges -> paths):
#   api/       — FastAPI-роутеры (единственная точка входа HTTP/WS)
#   services/  — прикладная логика: safe-пайплайн, аутентификация, релей алертов
#   domain/    — клиническая логика: rules (декларативные хард-стопы, SSOT),
#                routing (маршрутизация 3185-1), trajectory (24D, К1-К5), evidence_xai
#   bridges/   — единственный слой FFI к libcompass_core.so (ctypes), один загрузчик
#   db/        — схема, контекст соединений (RLS), seed
#   rules/     — декларативные JSON-правила (данные, не код)
#   paths.py   — карта путей репозитория

# Минимальный загрузчик .env (без python-dotenv): KEY=VALUE из корня репо попадают
# в os.environ ДО импортов модулей, чтобы fail-closed секреты (JWT_SECRET,
# PII_PEPPER) работали и в dev-запусках. Существующие переменные НЕ перезаписываются.
import os as _os
from pathlib import Path as _Path

_env_file = _Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _os.environ.setdefault(_k.strip(), _v.strip())
