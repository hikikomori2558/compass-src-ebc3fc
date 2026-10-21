# benchmarks/benchmark_c_batch_speed.py
"""
Экстремальный стресс-тест Метрики №1 (Быстродействие) и Метрики №6 (Устойчивость к нагрузкам):
Прогон 10 000 пациентов (1 680 000 почасовых точек ОДУ Рунге-Кутты 4-го порядка + СКФ + Чайлд-Пью + ОХЛП)
через нативное C11-ядро libcompass_core.so.
"""
import ctypes
import time
from pathlib import Path

from backend.bridges.core import CCompassCoreOutput, CPatientClinicalInput, RapiraCCoreBridge

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_10k_stress_benchmark(batch_size: int = 10000) -> None:
    bridge = RapiraCCoreBridge()

    # Выделяем непрерывный блок памяти под 10 000 входных и выходных структур C11
    InArrayType = CPatientClinicalInput * batch_size
    OutArrayType = CCompassCoreOutput * batch_size
    in_batch = InArrayType()
    out_batch = OutArrayType()

    for i in range(batch_size):
        in_batch[i].age = 25 + (i % 50)
        in_batch[i].sex = i % 2
        in_batch[i].days_since_maoi = 255
        in_batch[i].serum_creatinine_umol_l = 65.0 + (i % 95)
        in_batch[i].bilirubin_umol_l = 12.0
        in_batch[i].albumin_g_l = 42.0
        in_batch[i].inr = 1.05
        in_batch[i].cyp2d6_phenotype = i % 3
        in_batch[i].tsh_mIU_l = 1.9
        in_batch[i].ferritin_ug_l = 55.0
        in_batch[i].hemoglobin_g_l = 138.0
        in_batch[i].ecg_qtc_ms = 420.0

    has_batch_fn = hasattr(bridge.lib, "evaluate_compass_batch_v26")
    if has_batch_fn:
        bridge.lib.evaluate_compass_batch_v26.argtypes = [
            ctypes.POINTER(CPatientClinicalInput),
            ctypes.POINTER(CCompassCoreOutput),
            ctypes.c_int,
        ]
        bridge.lib.evaluate_compass_batch_v26.restype = None

        t0 = time.perf_counter()
        bridge.lib.evaluate_compass_batch_v26(in_batch, out_batch, batch_size)
        total_ms = (time.perf_counter() - t0) * 1000.0
        mode_name = "C11 Native Batch Vectorized (Zero-GIL)"
    else:
        t0 = time.perf_counter()
        for i in range(batch_size):
            bridge.lib.evaluate_compass_core_v26(ctypes.byref(in_batch[i]), ctypes.byref(out_batch[i]))
        total_ms = (time.perf_counter() - t0) * 1000.0
        mode_name = "C11 Single-Call Loop via ctypes"

    per_patient_ms = total_ms / batch_size
    throughput_rps = int((batch_size / (total_ms / 1000.0)))

    print("=" * 84)
    print(f"СТРЕСС-ТЕСТ C11 ЯДРА ({mode_name})")
    print("=" * 84)
    print(f"Объем пакета:                  {batch_size:,} пациентов (1,680,000 точек RK4 ОДУ)")
    print(f"Суммарное время расчета:       {total_ms:.2f} мс")
    print(f"Среднее время на 1 пациента:   {per_patient_ms:.5f} мс ({per_patient_ms * 1000:.2f} микросекунд!)")
    print(f"Пиковая пропускная способность: {throughput_rps:,} пациентов/сек (RPS на 1 ядре CPU)")
    print("=" * 84)


if __name__ == "__main__":
    run_10k_stress_benchmark(10000)
