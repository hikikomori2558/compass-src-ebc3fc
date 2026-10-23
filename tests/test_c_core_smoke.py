#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Смоук-тест и бенчмарк C-Core (Метрика №1: целевой отклик нативного вызова < 0.15 мс).
Запуск из корня репо: python3 -m tests.test_c_core_smoke  (сначала: python3 scripts/build_c_core.py)
"""

import ctypes

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_PATH = ROOT / "c_core" / "libcompass_core.so"

if __name__ == "__main__":
    if not LIB_PATH.exists():
        print(f"[!] Библиотека {LIB_PATH.name} еще не собрана.")
        print("    Запусти: python3 scripts/build_c_core.py (или VS Code: Cmd+Shift+B)")
        raise SystemExit(0)

    print(f"[OK] Библиотека найдена: {LIB_PATH}")

    from backend.bridges.core import CCompassCoreOutput, CPatientClinicalInput, RapiraCCoreBridge

    bridge = RapiraCCoreBridge()
    c_in = CPatientClinicalInput(
        age=36, sex=1, serum_creatinine_umol_l=86.0, cyp2d6_phenotype=2,
    )
    c_out = CCompassCoreOutput()
    call = bridge.lib.evaluate_compass_core_v26

    # Прогрев (первые вызовы несут накладные расходы разрешения символов)
    for _ in range(100):
        call(ctypes.byref(c_in), ctypes.byref(c_out))

    latencies = []
    for _ in range(10_000):
        t0 = time.perf_counter()
        call(ctypes.byref(c_in), ctypes.byref(c_out))
        latencies.append((time.perf_counter() - t0) * 1000.0)

    latencies.sort()
    mean_ms = sum(latencies) / len(latencies)
    p50_ms = latencies[len(latencies) // 2]
    p95_ms = latencies[int(len(latencies) * 0.95)]
    p99_ms = latencies[int(len(latencies) * 0.99)]
    print(f"[OK] Нативный вызов evaluate_compass_core_v26 (10 000 прогонов):")
    print(f"     среднее {mean_ms:.4f} мс | p50 {p50_ms:.4f} мс | p95 {p95_ms:.4f} мс | p99 {p99_ms:.4f} мс")
    print(f"[OK] Метрика №1 (нативное ядро < 0.15 мс): {'ПРОЙДЕНА' if p95_ms < 0.15 else 'ПРОВАЛЕНА'}")
    print("[i] Полный конвейер с Python-обвязкой (dict + округление 336 значений PK) замеряется отдельно в бенчмарке 100 ЭМК.")
