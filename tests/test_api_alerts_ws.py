#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ws_latency.py — шина экстренного алертинга (день 5, блок 2; Условия 5–6).

Прогон: python3 benchmarks/test_ws_latency.py (нужны docker compose up -d db redis + seed).

Проверяем обещания, а не «не падает»:
  * сквозная латентность чек-ин -> кадр в сокете врача: цель < 50 мс (10 прогонов,
    первый — прогревочный, не считаем);
  * в канале ПУСТОЙ ПИНГ: в кадре нет ни жалоб, ни шкал, ни диагнозов — только alert_id;
  * идемпотентность: повторный event_uuid не рождает второй алерт (офлайн-буфер PWA);
  * «время до подтверждения человеком» замеряется и пишется в delivery-журнал;
  * бэклог: врач, зашедший после алерта, получает красный баннер неподтверждённых;
  * delivery-журнал и delivery_ms заполнены (транспортная телеметрия).
"""
import asyncio
import atexit
import statistics
import sys
import time
import uuid
from contextlib import ExitStack

import asyncpg
from fastapi.testclient import TestClient

from backend.main import app

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


_stack = ExitStack()
c = _stack.enter_context(TestClient(app))
atexit.register(_stack.close)

# --- Фундамент: логин врача, приглашение, привязка пациента (как в test_auth) ---
doc = c.post("/api/v1/auth/login",
             json={"login": "neuro.ivlanov", "password": "compass-demo-2026"}).json()
DHDR = {"Authorization": f"Bearer {doc['access_token']}"}
DOC_ID = doc["principal_sub"]

# Прогоны накапливают неподтверждённые алерты у одного и того же демо-врача.
# Чистим их же эндпоинтом подтверждения — заодно лишний раз проверяем маршрут.
for a in c.get("/api/v1/alerts/unconfirmed", headers=DHDR).json()["alerts"]:
    c.post(f"/api/v1/alerts/{a['alert_id']}/confirm", headers=DHDR,
           json={"action_taken": "сброс тестового окружения между прогонами"})

inv = c.post("/api/v1/auth/invite", headers=DHDR).json()
pat = c.post("/api/v1/auth/bind", json={"token": inv["token"]}).json()
PHDR = {"Authorization": f"Bearer {pat['access_token']}"}

latencies = []
frames = []
PING_KEYS_ALLOWED = {"kind", "alert_id"}   # ВСЁ, что разрешено в канале
first_alert = None

# Сессия WebSocket обязана жить в контекст-менеджере — иначе у тест-сессии нет portal
# (см. starlette/testclient.py: portal появляется только в __enter__).
with c.websocket_connect(f"/ws/alerts/{DOC_ID}?token={doc['access_token']}") as ws:
    hello = ws.receive_json()
    check("HELLO при подключении, бэклог пуст (врач свежий)",
          hello["kind"] == "HELLO" and hello["unconfirmed"] == [])

    for i in range(10):
        ev = str(uuid.uuid4())
        t0 = time.perf_counter()
        r = c.post("/api/v1/patient/checkin", headers=PHDR,
                   json={"event_uuid": ev, "cssrs": 4, "phq9_total": 21, "phq9_item9": 2})
        assert r.status_code == 201, r.text
        frame = ws.receive_json()
        dt = (time.perf_counter() - t0) * 1000
        frames.append(frame)
        if i == 0:
            first_alert = frame["alert_id"]   # прогрев: создание группы, коннекты
        else:
            latencies.append(dt)

    mean_ms = statistics.mean(latencies)
    max_ms = max(latencies)
    print(f"[i] Чек-ин -> кадр врачу: среднее {mean_ms:.1f} мс, максимум {max_ms:.1f} мс (9 замеров)")
    check("Сквозная латентность < 50 мс (среднее)", mean_ms < 50, f"{mean_ms:.1f} мс")
    check("Худший прогон < 100 мс", max_ms < 100, f"{max_ms:.1f} мс")
    check("Кадр — ПУСТОЙ ПИНГ: только kind и alert_id",
          all(set(f.keys()) <= PING_KEYS_ALLOWED and f["kind"] == "CRITICAL_PING" for f in frames),
          str(frames[0]))
    check("alert_id во всех кадрах валидны", all(isinstance(f.get("alert_id"), int) for f in frames))

    # Идемпотентность: тот же event_uuid -> DUPLICATE (кадра не будет — сокет молчит)
    r = c.post("/api/v1/patient/checkin", headers=PHDR,
               json={"event_uuid": ev, "cssrs": 4, "phq9_total": 21, "phq9_item9": 2})
    check("Повторный event_uuid -> 200 DUPLICATE",
          r.status_code == 200 and r.json()["status"] == "DUPLICATE")

    # Подтверждение человеком: метрика Условий 5/6
    r = c.post(f"/api/v1/alerts/{first_alert}/confirm", headers=DHDR,
               json={"action_taken": "Связался с пациентом, маршрут к психиатру согласован"})
    check("Confirm: 200 + секунды до подтверждения человеком",
          r.status_code == 200 and r.json()["confirmed"] is True
          and "seconds_to_human_confirmation" in r.json(), r.text[:120])

    # Чужой врач не подтверждает чужой алерт (RLS)
    doc2 = c.post("/api/v1/auth/login",
                  json={"login": "psych.petrova", "password": "compass-demo-2026"}).json()
    r = c.post(f"/api/v1/alerts/{first_alert}/confirm",
               headers={"Authorization": f"Bearer {doc2['access_token']}"},
               json={"action_taken": "попытка чужого алерта"})
    check("Чужой врач не подтверждает чужой алерт -> 404 (RLS)", r.status_code == 404)

# --- Бэклог: повторный вход показывает неподтверждённые (красный баннер) ---
with c.websocket_connect(f"/ws/alerts/{DOC_ID}?token={doc['access_token']}") as ws2:
    hello2 = ws2.receive_json()
    unconfirmed_backlog = [a for a in hello2["unconfirmed"] if a != first_alert]
    check("Бэклог при повторном входе содержит неподтверждённые",
          len(unconfirmed_backlog) >= 9, f"{len(unconfirmed_backlog)} шт.")
    r = c.get("/api/v1/alerts/unconfirmed", headers=DHDR)
    check("REST /alerts/unconfirmed согласован с бэклогом WS",
          r.json()["count"] == len(hello2["unconfirmed"]))

# --- Не-критический чек-ин не будит шину ---
r = c.post("/api/v1/patient/checkin", headers=PHDR,
           json={"event_uuid": str(uuid.uuid4()), "phq9_total": 7, "gad7_total": 12})
check("Спокойный чек-ин: 201, не критичный", r.status_code == 201 and r.json()["critical"] is False)


# --- Состояние транспорта в БД ---
async def db_checks():
    conn = await asyncpg.connect("postgresql://compass_app:compass_demo@localhost:5433/compass")
    try:
        n_outbox = await conn.fetchval(
            "SELECT count(*) FROM critical_events_outbox WHERE published_to_redis")
        n_deliv = await conn.fetchval(
            "SELECT count(*) FROM notification_deliveries WHERE channel='WEBSOCKET' AND delivered_at IS NOT NULL")
        n_ms = await conn.fetchval(
            "SELECT count(*) FROM clinical_alerts_and_routing "
            "WHERE delivered_to_doctor_at IS NOT NULL AND delivery_ms IS NOT NULL")
        n_conf = await conn.fetchval(
            "SELECT count(*) FROM notification_deliveries WHERE confirmed_by_doctor_at IS NOT NULL")
        check("Outbox: записи доставлены в Redis (published)", n_outbox >= 10, f"{n_outbox}")
        check("Delivery-журнал заполнен (EMPTY_PING)", n_deliv >= 10, f"{n_deliv}")
        check("delivery_ms зафиксирован у доставленных", n_ms >= 10, f"{n_ms}")
        check("Подтверждение человека в журнале", n_conf >= 1, f"{n_conf}")
    finally:
        await conn.close()


asyncio.run(db_checks())

print()
if FAILURES:
    print(f"[FAIL] Провалено {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("[OK] Все проверки шины алертинга пройдены.")
