#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
load_probe.py — быстрый нагрузочный зонд API (RPS + перцентили, без внешних зависимостей).

Зачем свой, а не k6/Locust: однофайловый, работает на любой машине команды и на
Debian-ноде из коробки (только httpx из requirements), честно печатает p50/p95/p99
и фактический RPS — на слайд метрики №6 идут замеры, а не «до 15 000 RPS» из рекламы.

Прогон:
  python3 scripts/load_probe.py --url http://127.0.0.1:8000 --scenario health \
      --concurrency 64 --seconds 15
  python3 scripts/load_probe.py --scenario analyze --concurrency 32 --seconds 15
Сценарии:
  health  — GET /healthz (потолок инфраструктуры: uvicorn + event loop);
  analyze — POST /api/v1/compass-bio/ehr/analyze-and-route (боевой путь: NLP + C-ядро).

Совместимость: py3.9+ (asyncio.gather, не TaskGroup) — одинаково работает в venv
на macOS и на Debian-ноде.
"""
import argparse
import asyncio
import statistics
import time
from collections import Counter

import httpx

DEMO_EHR = (
    "Осмотр невролога (ф. 025/у). М, 36 г. Код МКБ: F43.2. "
    "Жал.: трев., инсомния после COVID-19, водит авто. "
    "Лаб: креат. 86 мкмоль/л, Чайлд-Пью: норма. "
    "Шкалы: ГТР-7 12б, PHQ9 7б (п9=0), CSSRS=0."
)


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(int(len(sorted_vals) * p), len(sorted_vals) - 1)]


async def hammer(client, url, method, body, deadline, latencies, errors):
    """Рабочий цикл одного виртуального пользователя до дедлайна.
    5xx и сетевые сбои — в errors; 4xx считаем латентностями (это наши ошибки в сценарии)."""
    while time.monotonic() < deadline:
        t0 = time.perf_counter()
        try:
            if method == "GET":
                r = await client.get(url)
            else:
                r = await client.post(url, json=body)
            dt = (time.perf_counter() - t0) * 1000
            if r.status_code < 500:
                latencies.append(dt)
            else:
                errors.append(str(r.status_code))
        except Exception as e:  # noqa: BLE001 — сетевые сбои это данные, не повод падать
            errors.append(type(e).__name__)


async def run(args) -> None:
    url = f"{args.url}/healthz" if args.scenario == "health" \
        else f"{args.url}/api/v1/compass-bio/ehr/analyze-and-route"
    method = "GET" if args.scenario == "health" else "POST"
    body = {"raw_ehr_text": DEMO_EHR}

    limits = httpx.Limits(max_connections=args.concurrency * 2,
                          max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(timeout=10, limits=limits) as client:
        r = await client.get(f"{args.url}/healthz")
        if r.status_code != 200:
            raise SystemExit(f"API недоступен: {args.url} -> {r.status_code}")

        latencies, errors = [], []
        t_start = time.monotonic()
        deadline = t_start + args.seconds
        await asyncio.gather(*[
            hammer(client, url, method, body, deadline, latencies, errors)
            for _ in range(args.concurrency)
        ])
        wall = time.monotonic() - t_start

    latencies.sort()
    n = len(latencies)
    print(f"[i] Сценарий {args.scenario}: {args.concurrency} потоков, {args.seconds:.0f} с, wall {wall:.0f} с")
    err_line = ""
    if errors:
        top, cnt = Counter(errors).most_common(1)[0]
        err_line = f" | ошибок: {len(errors)} ({top} x{cnt})"
    print(f"    запросов: {n} | RPS: {n / wall:.0f}{err_line}")
    if n:
        print(f"    p50 {percentile(latencies, 0.50):.1f} мс | p95 {percentile(latencies, 0.95):.1f} мс"
              f" | p99 {percentile(latencies, 0.99):.1f} мс | max {latencies[-1]:.1f} мс"
              f" | среднее {statistics.mean(latencies):.1f} мс")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--scenario", choices=["health", "analyze"], default="health")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--seconds", type=float, default=15)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
