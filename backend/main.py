# backend/main.py — главный FastAPI-сервер COMPASS-HYBRID v2.6.
# Запуск из корня репо:  python3 -m backend.main   (или: uvicorn backend.main:app)
# Инфраструктура: docker compose up -d  (PostgreSQL 16 + Redis 7 AOF)
import os
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api.alerts import alerts_router, ws_router
from backend.api.auth import auth_router
from backend.api.bio import bio_router
from backend.api.cdss_workstation import router as cdss_router


def _print_banner(pg_ok: bool, redis_ok: bool) -> None:
    """Стартовая сводка: режим стенда, адреса и что доступно. Печатается один раз."""
    full = pg_ok and redis_ok
    mode = "\033[1;32mПОЛНЫЙ (БД + алерты + QR-привязка)\033[0m" if full \
        else "\033[1;33mDEGRADED — без БД: вход/алерты недоступны, АРМ на офлайн-фоллбэке\033[0m"
    lan = ""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        lan = (out.stdout.split() or [""])[0]
    except Exception:
        pass

    def row(label: str, local: str, lan_url: str = "") -> str:
        line = f"\033[1;36m║\033[0m  {label:<14}\033[0m {local}"
        if lan_url:
            pad = max(1, 34 - len(label) - len(local))
            line += " " * pad + f"\033[2m| с хоста: {lan_url}\033[0m"
        return line + "\n"

    print(
        "\n\033[1;36m╔════════════════════════════════════════════════════════════════╗\033[0m\n"
        "\033[1;36m║\033[0m  \033[1mCOMPASS-HYBRID v2.6\033[0m · CDSS «Клинический компас» (команда «Rapira»)\n"
        "\033[1;36m║\033[0m  Режим: " + mode + "\n"
        "\033[1;36m╠════════════════════════════════════════════════════════════════╣\033[0m\n"
        + row("МИС-стенд:", "http://127.0.0.1:8000/", f"http://{lan}:8000/" if lan else "")
        + row("АРМ врача:", "http://127.0.0.1:8000/static/arm/", f"http://{lan}:8000/static/arm/" if lan else "")
        + row("PWA пациента:", "http://127.0.0.1:8000/static/pwa/", f"http://{lan}:8000/static/pwa/" if lan else "")
        + row("Swagger API:", "http://127.0.0.1:8000/docs", f"http://{lan}:8000/docs" if lan else "")
        + "\033[1;36m║\033[0m  Смоук API:    scripts/api_smoke.sh\n"
        + ("\033[1;36m║\033[0m  \033[2mВход демо: neuro.ivlanov / compass-demo-2026 (после seed)\033[0m\n" if full else
           "\033[1;36m║\033[0m  Полный режим:  docker compose up -d  ->  python3 -m backend.db.seed\n")
        + "\033[1;36m╚════════════════════════════════════════════════════════════════╝\033[0m"
    )

TELEMETRY_RING = deque(maxlen=500)
START_TS = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # БД и Redis подключаем по-доброму: нет стека — сервер всё равно встаёт и отвечает
    # биоэндпоинтами (полезно для быстрых прогонов без docker compose).
    app.state.pg_pool = None
    app.state.redis = None
    # Клиент к «проприетарному» серверу ЭМК вендора (порт 8100, отдельный процесс)
    app.state.emc_client = httpx.AsyncClient(timeout=10.0)
    dsn = os.getenv(
        "DATABASE_URL",
        # 5433 — локальная конвенция репо (docker-compose.override.yml): 5432 на
        # машинах разработчиков часто занят чужим postgres. В docker-сети api ходит на db:5432.
        "postgresql://compass_api:compass_api_demo@localhost:5433/compass",
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

    # Релей критических алертов (Postgres outbox -> Redis Streams). Живёт, пока жив
    # процесс: упал — стартанёт заново вместе с приложением, ничего не теряя (outbox).
    from backend.services.alerts import AlertRelay

    app.state.alert_relay = AlertRelay()
    if app.state.pg_pool is not None and app.state.redis is not None:
        await app.state.alert_relay.start(app.state.pg_pool, app.state.redis)
        print("[main] Alert relay: запущен (outbox -> Redis Streams)")

    _print_banner(pg_ok=app.state.pg_pool is not None, redis_ok=app.state.redis is not None)
    yield
    if getattr(app.state, "alert_relay", None):
        await app.state.alert_relay.stop()
    if getattr(app.state, "emc_client", None):
        await app.state.emc_client.aclose()
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
# no-cache для статики (/static + /emc) — внутри telemetry_middleware ниже, единая точка.
@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    # Каждый запрос сам себе секундомер: латентность уходит в кольцо и в заголовок ответа,
    # откуда её забирает SLA-панель интерфейса врача (Метрики №1 и №6).
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000.0
    TELEMETRY_RING.append({"path": request.url.path, "method": request.method, "ms": round(ms, 3), "status": response.status_code})
    response.headers["x-latency-ms"] = f"{ms:.3f}"
    # Статика стенда должна ВСЕГДА проверяться на свежесть: демо активно правится,
    # а браузер/Cloudflare без этого держат старые agent.js/sidebar.js вечно —
    # выглядит как «ничего не работает» после каждого обновления (было 23-24.10).
    if request.url.path.startswith(("/static/", "/emc")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["ETag"] = f'"{ms:.0f}-{request.url.path}"'  # меняется каждый ответ
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
app.include_router(auth_router)
app.include_router(alerts_router)
app.include_router(ws_router)
app.include_router(cdss_router)

# ------------------------------------------------------------------ стека демо
# Архитектура «в реальных условиях» (по требованию условия кейса):
#   * ЭМК — ПРОПРИЕТАРНАЯ система вендора: отдельный процесс backend_emc/server.py
#     (порт 8100, свой API /api/emc/*, своя стatica frontend_emc/). Ноль кода COMPASS.
#   * COMPASS — внешний слой: frontend_compass/ (install-хаб + agent.js + сайдбар),
#     читает данные С ЭКРАНА ЭМК и говорит с нашим API.
#   * Ниже: стatica COMPASS + прозрачный прокси к вендорскому серверу ЭМК, чтобы
#     публичный домен (cloudflared -> 8000) обслуживал обе системы одним портом.
import httpx

EMC_UPSTREAM = os.getenv("EMC_UPSTREAM", "http://127.0.0.1:8100")
_ROOT = Path(__file__).resolve().parent.parent
_COMPASS_STATIC = _ROOT / "frontend_compass"
if _COMPASS_STATIC.is_dir():
    app.mount("/static/compass", StaticFiles(directory=str(_COMPASS_STATIC), html=True), name="compass")

_PWA_STATIC = _ROOT / "frontend_patient"
if _PWA_STATIC.is_dir():
    # html=True обязателен: QR-приглашение ведёт на /static/pwa/?token=… (без имени
    # файла) — без него телефон пациента получал {"detail":"Not Found"}.
    app.mount("/static/pwa", StaticFiles(directory=str(_PWA_STATIC), html=True), name="pwa")


async def _emc_proxy(request: Request, path: str) -> Response:
    """Прозрачный прокси к серверу ЭМК вендора (публичный доступ одним доменом)."""
    url = f"{EMC_UPSTREAM}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    client = request.app.state.emc_client
    upstream = await client.request(request.method, url, headers=headers,
                                    content=await request.body())
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers={k: v for k, v in upstream.headers.items()
                             if k.lower() not in ("content-encoding", "transfer-encoding",
                                                  "content-length", "connection")},
                    media_type=upstream.headers.get("content-type"))


@app.api_route("/api/emc/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
async def emc_api_proxy(path: str, request: Request) -> Response:
    return await _emc_proxy(request, f"api/emc/{path}")


@app.api_route("/emc/{path:path}", methods=["GET"], include_in_schema=False)
async def emc_static_proxy(path: str, request: Request) -> Response:
    # префикс /emc срезается: у вендора статика смонтирована в корне
    return await _emc_proxy(request, path)


@app.api_route("/emc", methods=["GET"], include_in_schema=False)
async def emc_root_proxy(request: Request) -> Response:
    return await _emc_proxy(request, "")


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse("/static/compass/install.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
