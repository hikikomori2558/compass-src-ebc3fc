#!/usr/bin/env python3
# load_test.py — честный нагрузочный профиль API (Метрики №1 и №6).
#
# Три сценария на живом сервере:
#   healthz   — верхняя граница транспорта (лёгкий эндпоинт, без БД/ядра);
#   analyze   — боевой путь врача: NLP ЭМК + C-ядро + правила + XAI + CBR;
#   encounter — АРМ-путь: Bitap NER + RK45 PopPK + DST + микросети + Оуэн.
#
# Меряем на одном воркере uvicorn (честно подписываем). Результаты:
#   benchmarks/load_results.json + benchmarks/plots/fig6_load_profile.png
#
# Запуск: scripts/load_test.py [BASE_URL] [TOKEN_FILE]
#   (токен — честный вход по логину/паролю из окружения, как у реального врача)
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

SCENARIOS = [
    # (имя, путь, payload, число запросов, параллелизм)
    ("healthz", "/healthz", None, 2000, 64),
    ("analyze (боевой путь врача)", "/api/v1/compass-bio/ehr/analyze-and-route",
     {"raw_ehr_text": "Осмотр психиатра. Женщина, 38 лет. F41.1. Принимает пароксетин и кларитромицин. "
                      "Транилципромин отрицает. Креат. 86, Чайлд-Пью норма. ГТР-7 12б, PHQ9 7б (п9=0)."}, 300, 16),
    ("encounter (АРМ: RK45+DST+Оуэн)", "/api/v1/cdss/evaluate-encounter",
     {"ehr_note_text": "Пациентка принимает Авиандр, пароксетин и кларитромицин. Водит авто.",
      "cyp2d6_activity_score": 0.12}, 300, 16),
]

EHR_NOTE = SCENARIOS[1][2]["raw_ehr_text"]


async def worker(name, path, payload, token, n, sem, lat):
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(n):
            async with sem:
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                t0 = time.perf_counter()
                r = await client.post(BASE + path, content=json.dumps(payload) if payload else None,
                                      headers=headers)
                dt = (time.perf_counter() - t0) * 1000.0
                if r.status_code >= 500:
                    raise RuntimeError(f"{name}: HTTP {r.status_code}")
                lat.append(dt)


async def run_scenario(name, path, payload, total, conc, token):
    import httpx
    sem = asyncio.Semaphore(conc)
    lat = []
    workers = [worker(name, path, payload, token, total // conc, sem, lat) for _ in range(conc)]
    t0 = time.perf_counter()
    await asyncio.gather(*workers)
    wall = time.perf_counter() - t0
    lat.sort()
    n = len(lat)
    pct = lambda p: lat[min(int(n * p), n - 1)]
    return {
        "scenario": name, "requests": n, "concurrency": conc,
        "wall_s": round(wall, 2), "rps": round(n / wall, 1),
        "p50_ms": round(pct(0.50), 2), "p95_ms": round(pct(0.95), 2),
        "p99_ms": round(pct(0.99), 2), "max_ms": round(lat[-1], 2),
        "errors": total - n,
    }


async def main() -> int:
    import httpx

    async with httpx.AsyncClient() as c:
        r = await c.post(BASE + "/api/v1/auth/login", json={
            "login": os.getenv("COMPASS_LOGIN", "neuro.ivlanov"),
            "password": os.getenv("COMPASS_PASSWORD", "compass-demo-2026"),
        })
        r.raise_for_status()
        token = r.json()["access_token"]

    # прогрев C-ядра и соединений
    async with httpx.AsyncClient() as c:
        await c.get(BASE + "/healthz")
        await c.post(BASE + "/api/v1/compass-bio/ehr/analyze-and-route",
                     json={"raw_ehr_text": EHR_NOTE},
                     headers={"Authorization": "Bearer " + token})

    results = []
    for name, path, payload, total, conc in SCENARIOS:
        print(f"[..] {name}: {total} запросов, параллелизм {conc}")
        r = await run_scenario(name, path, payload, total, conc, token)
        results.append(r)
        print(f"[OK ] {name}: {r['rps']} rps | p50 {r['p50_ms']} мс | p95 {r['p95_ms']} мс | p99 {r['p99_ms']} мс")

    out = {"base_url": BASE, "workers": 1, "generated": time.strftime("%Y-%m-%d %H:%M"),
           "note": "один воркер uvicorn, одна виртуалка aarch64 — линейно масштабируется --workers",
           "results": results}
    (ROOT / "benchmarks" / "load_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # график профиля
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    fig.patch.set_facecolor("white")
    names = [r["scenario"].split(" (")[0] for r in results]
    x = np.arange(len(names))
    axes[0].bar(x - 0.2, [r["p50_ms"] for r in results], width=0.38, label="p50", color="#0284c7")
    axes[0].bar(x + 0.2, [r["p95_ms"] for r in results], width=0.38, label="p95", color="#dc2626")
    axes[0].set_yscale("log")
    axes[0].set_xticks(x, names, fontsize=9)
    axes[0].set_ylabel("Задержка, мс (лог)", fontsize=9, fontweight="bold")
    axes[0].set_title("Задержки ответов (p50/p95, лог-шкала)", fontsize=10, fontweight="bold")
    axes[0].legend(fontsize=9)
    axes[0].grid(True, axis="y", linestyle=":", alpha=0.6)
    for i, r in enumerate(results):
        axes[0].text(i - 0.2, r["p50_ms"] * 1.15, f'{r["p50_ms"]:.1f}', ha="center", fontsize=8)
        axes[0].text(i + 0.2, r["p95_ms"] * 1.15, f'{r["p95_ms"]:.0f}', ha="center", fontsize=8)

    axes[1].bar(names, [r["rps"] for r in results], color="#16a34a", width=0.5)
    axes[1].set_title(f"Пропускная способность (1 воркер uvicorn, {results[0]['concurrency']} соединений)",
                      fontsize=10, fontweight="bold")
    axes[1].set_ylabel("Запросов/сек", fontsize=9, fontweight="bold")
    axes[1].grid(True, axis="y", linestyle=":", alpha=0.6)
    for i, r in enumerate(results):
        axes[1].text(i, r["rps"] * 1.02, f'{r["rps"]:.0f} rps', ha="center", fontsize=9, fontweight="bold")

    plt.tight_layout()
    out_png = ROOT / "benchmarks" / "plots" / "fig6_load_profile.png"
    out_png.parent.mkdir(exist_ok=True)
    plt.savefig(out_png, dpi=300)
    plt.savefig(out_png.with_suffix(".svg"))
    print(f"[OK] график: {out_png.name} (PNG 300 DPI + SVG)")
    print(f"[OK] результаты: benchmarks/load_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
