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
