# backend/main.py — Главный FastAPI-сервер COMPASS-HYBRID v2.6 (Разработчик №1)
# Запуск из корня репо:  python3 backend/main.py   (или: uvicorn backend.main:app)
# Инфраструктура: docker compose up -d  (PostgreSQL 16 + Redis 7 AOF)
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

# Плоские импорты биоконтура (api_router_bio -> trajectory_clustering -> bio_paths)
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api_router_bio import bio_router  # noqa: E402

TELEMETRY_RING = deque(maxlen=500)
START_TS = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # БД и Redis подключаем по-доброму: нет стека — сервер всё равно встаёт и отвечает
    # биоэндпоинтами (полезно для быстрых прогонов без docker compose).
    app.state.pg_pool = None
    app.state.redis = None
    dsn = os.getenv(
        "DATABASE_URL",
        "postgresql://compass_api:compass_api_demo@localhost:5432/compass",
    )
    if os.getenv("ENABLE_DB", "1") == "1":
        try:
            import asyncpg

            app.state.pg_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8, command_timeout=5)
            async with app.state.pg_pool.acquire() as c:
                await c.fetchval("SELECT 1")
            print("[main] PostgreSQL: подключено")
        except Exception as e:  # noqa: BLE001
            print(f"[main] PostgreSQL недоступен (не критично): {e!r}")
    try:
        from redis.asyncio import from_url

        r = from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), socket_connect_timeout=2)
        await r.ping()
        app.state.redis = r
        print("[main] Redis: подключено")
    except Exception as e:  # noqa: BLE001
        print(f"[main] Redis недоступен (не критично): {e!r}")
    yield
    if app.state.pg_pool:
        await app.state.pg_pool.close()
    if app.state.redis:
        await app.state.redis.aclose()


app = FastAPI(
    title="COMPASS-HYBRID v2.6 — Rapira CDSS (Main API)",
    version="2.6.0",
    description="Светофор маршрутизации Авиандр®: C-Core + 24D кластеры + NLP ЭМК + WebSocket-алертинг",
    lifespan=lifespan,
)
# Порядок важен (доки FastAPI): последняя добавленная middleware — самая внешняя.
# Поэтому сначала телеметрия, а CORS — последним: тогда CORS-заголовки есть и на
# ответах телеметрии/ошибок, а не только на счастливом пути маршрутов.
@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    # Каждый запрос сам себе секундомер: латентность уходит в кольцо и в заголовок ответа,
    # откуда её забирает SLA-панель интерфейса врача (Метрики №1 и №6).
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000.0
    TELEMETRY_RING.append({"path": request.url.path, "method": request.method, "ms": round(ms, 3), "status": response.status_code})
    response.headers["x-latency-ms"] = f"{ms:.3f}"
    return response


app.add_middleware(
    CORSMiddleware,
    # Дефолт «*» — это демо-режим чемпионата: сайдбар врача живёт на другом порту.
    # В бою сюда приходит список доменов МИС через CORS_ORIGINS, и «*» запрещён.
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    # без expose_headers браузер наш кастомный x-latency-ms просто не покажет (доки CORS)
    expose_headers=["x-latency-ms"],
)


@app.get("/healthz", tags=["system"])
async def healthz():
    pg = redis = "down"
    if app.state.pg_pool:
        try:
            async with app.state.pg_pool.acquire() as c:
                await c.fetchval("SELECT 1")
            pg = "ok"
        except Exception:  # noqa: BLE001
            pass
    if app.state.redis:
        try:
            await app.state.redis.ping()
            redis = "ok"
        except Exception:  # noqa: BLE001
            pass
    return {
        "status": "ok" if (pg == "ok" and redis == "ok") else "degraded",
        "pg": pg,
        "redis": redis,
        "uptime_s": round(time.time() - START_TS, 1),
    }


@app.get("/api/v1/telemetry/summary", tags=["system"])
async def telemetry_summary():
    """Живая телеметрия для SLA-панели интерфейса врача (Метрики №1, №6)."""
    ms = sorted(item["ms"] for item in TELEMETRY_RING)
    n = len(ms)
    return {
        "requests_observed": n,
        "p50_ms": ms[n // 2] if n else None,
        "p95_ms": ms[int(n * 0.95)] if n else None,
        "p99_ms": ms[min(int(n * 0.99), n - 1)] if n else None,
        "recent": list(TELEMETRY_RING)[-20:],
    }


app.include_router(bio_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
