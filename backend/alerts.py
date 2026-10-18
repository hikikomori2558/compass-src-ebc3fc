# backend/alerts.py — релей критических алертов: Postgres (истина) -> Redis Streams (транспорт).
#
# Почему outbox, а не «XADD прямо из эндпоинта»: выписка и отправка в двух системах —
# это классическая потеря сообщения (упал Redis в момент коммита — алерт о суицидальном
# риске исчез). Паттерн Transactional Outbox: запись об алерте и запись в outbox летят
# В ОДНОЙ транзакции Postgres, а релей уже гарантированно довозит её до Redis. Источник
# истины — Postgres; Redis — только транспорт (требование чек-листа 3.7).
#
# Что летит в канал: ПУСТОЙ ПИНГ (решение 0-bis №2) — только alert_id и время.
# Ни жалоб, ни шкал, ни диагнозов: канал (WebSocket/пуш) может утечь, карточка — нет.
# Клиникa по alert_id достаётся внутри контура, авторизованным врачом.
import asyncio
import json
from typing import Any, Dict, List, Optional

import asyncpg
import redis.asyncio as aioredis
from redis import ResponseError

STREAM_PREFIX = "alerts:"          # alerts:{doctor_id} — по стриму на врача
STREAM_GROUP = "doctors"           # одна группа на стрим; consumer = соединение
STREAM_MAXLEN = 10_000             # журнал доставки врачу; старое режем приблизительно


def stream_name(doctor_id: str) -> str:
    return f"{STREAM_PREFIX}{doctor_id}"


async def ensure_group(r: aioredis.Redis, doctor_id: str) -> None:
    """Группа создаётся при первом подключении врача. BUSYGROUP — не ошибка,
    а «уже создана»: конкурентные подключения гоняются за созданием честно."""
    try:
        await r.xgroup_create(stream_name(doctor_id), STREAM_GROUP, id="$", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


class AlertRelay:
    """Переносит committed-записи из critical_events_outbox в стримы врачей.

    Два режима подстёгивания:
      * kick() — эндпоинт после коммита алерта будит релей сразу (латентность ~мс);
      * периодический проход раз в SWEEP_INTERVAL — страховка, если kick потерялся
        (например, релей перезапускался ровно между коммитом и kick).
    FOR UPDATE SKIP LOCKED — два релея (или standalone-процесс на втором узле,
    день 9) не съедают одну запись дважды и не блокируют друг друга.
    """

    SWEEP_INTERVAL = 0.2  # сек

    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._pool: Optional[asyncpg.Pool] = None
        self._redis: Optional[aioredis.Redis] = None
        self.stats = {"published": 0, "sweeps": 0}

    # ------------------------------------------------------------ жизненный цикл
    async def start(self, pool: asyncpg.Pool, redis: aioredis.Redis) -> None:
        self._pool, self._redis = pool, redis
        self._task = asyncio.create_task(self._run(), name="alert-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def kick(self) -> None:
        """Разбудить релей: алерт закоммичен, доставка — сейчас, а не через sweep."""
        self._wake.set()

    # ------------------------------------------------------------ перенос
    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._drain_once()
                except Exception as e:  # noqa: BLE001 — релей не имеет права умирать
                    print(f"[alert-relay] проход не удался ({e!r}), продолжаю")
                self.stats["sweeps"] += 1
                # ждём kick, но не дольше интервала страховки
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.SWEEP_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
        except asyncio.CancelledError:
            raise

    async def _drain_once(self) -> None:
        assert self._pool is not None and self._redis is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_id, payload FROM critical_events_outbox "
                "WHERE NOT published_to_redis "
                "ORDER BY event_id LIMIT 64 FOR UPDATE SKIP LOCKED"
            )
            for row in rows:
                payload: Dict[str, Any] = json.loads(row["payload"])
                # адресаты фиксируются в момент выписки алерта (снапшот привязок в той же
                # транзакции, что и сам алерт) — рассинхрона привязок при доставке нет
                for doctor_id in payload.get("doctor_ids", []):
                    # в стрим — только пустой пинг: alert_id и время. Всё.
                    await self._redis.xadd(
                        stream_name(doctor_id),
                        {"alert_id": str(payload["alert_id"]), "ts": str(payload["ts"])},
                        maxlen=STREAM_MAXLEN,
                        approximate=True,
                    )
                await conn.execute(
                    "UPDATE critical_events_outbox SET published_to_redis = true WHERE event_id = $1",
                    row["event_id"],
                )
                self.stats["published"] += 1


if __name__ == "__main__":
    # Standalone-режим (второй узел в день 9): python3 backend/alerts.py
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    async def _standalone() -> None:
        pool = await asyncpg.create_pool(
            os.getenv("DATABASE_URL", "postgresql://compass_api:compass_api_demo@localhost:5433/compass"),
            min_size=1, max_size=4,
        )
        r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        relay = AlertRelay()
        await relay.start(pool, r)
        print("[alert-relay] standalone-релей запущен; Ctrl+C — выход")
        try:
            while True:
                await asyncio.sleep(10)
                print(f"[alert-relay] stats: {relay.stats}")
        finally:
            await relay.stop()
            await pool.close()
            await r.aclose()

    asyncio.run(_standalone())
