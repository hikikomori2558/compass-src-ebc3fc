# -*- coding: utf-8 -*-
"""
benchmarks/test_day5_bio_and_micronets.py
=========================================
Верификация нативного C11 био-контура (LPC-4 глоттальный фильтр, TKEO,
бимодальный русский Вейбулл, W2-адаптация Kintsugi DAM, Демпстер-Шефер),
C11 W8A8 движка микросетей с Mondrian RAPS и сквозного моста HybridBioControlBridge.
"""

from __future__ import annotations

import math

import numpy as np

from backend.bridges.bio_acoustic import NonlinearAcousticDSP
from backend.bridges.bio_fusion import DempsterShaferBioFusion
from backend.services.bio_control import HybridBioControlBridge
from micro_nets.train_distill_manifold import (
    build_clinical_manifold_dataset,
    run_full_control_plane_distillation,
)
from micro_nets.inference_engine import CompassControlPlaneEngine


def test_c11_lpc4_glottal_tkeo_and_russian_closures() -> None:
    """
    Проверяет, что C11 LPC-4 фильтр и бимодальный Вейбулл:
    1) Отделяют русские глухие смычки (40-85 мс) от когнитивных пауз (>110 мс).
    2) Корректно обрабатывают сырой PCM-сигнал через C11 compass_analyze_pcm_russian_invariant.
    """
    dsp = NonlinearAcousticDSP()
    rng = np.random.default_rng(42)

    closures = rng.uniform(45.0, 85.0, size=40)
    hesitations = 780.0 * rng.weibull(0.68, size=120) + 115.0
    mixed_pauses = np.concatenate([closures, hesitations])

    est_k, est_lam, plosive_ratio = dsp.fit_weibull_pause_mle(mixed_pauses)
    assert 0.20 <= plosive_ratio <= 0.30, f"Неверная доля русских смычек: {plosive_ratio}"
    assert 0.50 <= est_k <= 0.95, f"Смычки исказили параметр формы Вейбулла k: {est_k}"
    assert 600.0 <= est_lam <= 1150.0, f"Неверный масштаб Вейбулла lambda: {est_lam}"

    sr = 16000
    t = np.linspace(0.0, 1.0, sr, endpoint=False)
    pcm = 0.6 * (1.0 + 0.30 * np.sin(2.0 * np.pi * 8.0 * t)) * np.cos(2.0 * np.pi * 135.0 * t)
    bio_vec = dsp.extract_from_pcm(pcm, sample_rate=sr)
    assert bio_vec.f0_mean_hz > 80.0
    assert bio_vec.glottal_tkeo_instability > 0.01


def test_w2_cross_lingual_adaptation_prevents_false_positive_on_russian_norm() -> None:
    """
    Проверяет, что здоровая русская речь с характерным терминальным падением ИК-1
    и завышенным сырым скором англоязычной Kintsugi DAM (raw_dep=0.47, raw_anx=0.43)
    после W2-адаптации в C11 НЕ дает ложноположительного рецидива.
    """
    dsp = NonlinearAcousticDSP()
    fusion = DempsterShaferBioFusion()

    healthy_ru_bio = dsp.build_from_telemetry_summary({
        "f0_mean_hz": 128.0,
        "f0_std_hz": 17.5,
        "jitter_ppq5_pct": 0.42,
        "shimmer_apq5_pct": 2.05,
        "glottal_tkeo_instability": 0.17,
        "glottal_tkeo_kurtosis": 1.6,
        "weibull_pause_shape_k": 1.28,
        "weibull_pause_scale_lambda_ms": 300.0,
        "cognitive_pause_fraction": 0.15,
        "ru_plosive_closure_fraction": 0.29,
        "lpc_spectral_tilt_db": 7.8,
        "snr_db": 32.0,
    })

    report = fusion.evaluate_patient_multimodal(
        biomarkers=healthy_ru_bio,
        hama_total=5.0,
        madrs_total=6.0,
        patient_self_report_wellbeing=0.90,
        raw_kintsugi_dep_score=0.47,
        raw_kintsugi_anx_score=0.43,
        response_latency_sec=1.5,
    )
    assert report.dominant_hypothesis == "Remission", f"Ошибочная гипотеза на здоровой русской речи: {report}"
    assert report.clinical_phenotype_flag == "CONCORDANT_STABLE_TRAJECTORY"


def test_c11_dempster_shafer_dissimulation_detection() -> None:
    """Проверяет детекцию скрытой диссимуляции в C11-ядре."""
    dsp = NonlinearAcousticDSP()
    fusion = DempsterShaferBioFusion()

    pathological_bio = dsp.build_from_telemetry_summary({
        "f0_mean_hz": 115.0,
        "f0_std_hz": 6.0,
        "jitter_ppq5_pct": 1.72,
        "shimmer_apq5_pct": 5.60,
        "glottal_tkeo_instability": 0.54,
        "glottal_tkeo_kurtosis": 7.2,
        "weibull_pause_shape_k": 0.56,
        "weibull_pause_scale_lambda_ms": 950.0,
        "cognitive_pause_fraction": 0.45,
        "ru_plosive_closure_fraction": 0.25,
        "snr_db": 34.0,
    })

    report = fusion.evaluate_patient_multimodal(
        biomarkers=pathological_bio,
        hama_total=4.0,
        madrs_total=5.0,
        patient_self_report_wellbeing=0.96,
        raw_kintsugi_dep_score=0.86,
        raw_kintsugi_anx_score=0.89,
        response_latency_sec=4.4,
    )
    assert report.dempster_conflict_k > 0.40
    assert report.dissimulation_index >= 0.35
    assert report.clinical_phenotype_flag == "MASKED_RELAPSE_DISSIMULATION"
    assert math.isclose(sum(report.pignistic_prob.values()), 1.0, rel_tol=1e-4)


def test_c11_w8a8_micronets_conformal_coverage_and_submillisecond_latency() -> None:
    """Проверяет точность, конформное покрытие (>=99% на критическом классе) и скорость C11 W8A8."""
    distill_report = run_full_control_plane_distillation()
    assert distill_report["router_net"]["cal_acc"] >= 0.85

    engine = CompassControlPlaneEngine()
    X_test, y_router_test, _, _ = build_clinical_manifold_dataset(n_synthetic=300, seed=9999)

    covered = 0
    crit_total = 0
    crit_covered = 0
    latencies_ms = []

    for i in range(X_test.shape[0]):
        dec = engine.evaluate_patient_vector(X_test[i])
        latencies_ms.append(dec.total_inference_latency_ms)
        true_cls = int(y_router_test[i])
        if true_cls in dec.router_decision.prediction_set_indices:
            covered += 1
        if true_cls == 2:
            crit_total += 1
            if 2 in dec.router_decision.prediction_set_indices:
                crit_covered += 1

    emp_cov = covered / float(X_test.shape[0])
    crit_cov = crit_covered / float(max(1, crit_total))
    median_lat_ms = float(np.median(latencies_ms))

    assert emp_cov >= 0.96, f"Общее конформное покрытие ниже 96%: {emp_cov:.4f}"
    assert crit_cov >= 0.99, f"Покрытие критического класса ниже 99%: {crit_cov:.4f}"
    assert median_lat_ms < 1.0, f"Латентность C11 W8A8 трех микросетей должна быть < 1 мс: {median_lat_ms:.4f} мс"


def test_end_to_end_hybrid_bridge() -> None:
    """Интеграционный тест сквозного C11-моста HybridBioControlBridge."""
    bridge = HybridBioControlBridge()
    sr = 16000
    t = np.linspace(0.0, 1.2, int(sr * 1.2), endpoint=False)
    synth_voice = 0.5 * (1.0 + 0.35 * np.sin(2.0 * np.pi * 7.5 * t)) * np.cos(2.0 * np.pi * 130.0 * t)

    result = bridge.process_clinical_encounter(
        hama_total=6.0,
        madrs_total=7.0,
        patient_self_report_wellbeing=0.92,
        cyp2d6_activity_score=0.2,
        cyp3a4_inhibition_factor=2.1,
        pcm_audio=synth_voice,
        sample_rate=sr,
        raw_kintsugi_dep_score=0.84,
        raw_kintsugi_anx_score=0.87,
        response_latency_sec=4.5,
        hard_stop_triggered=True,
    )
    assert "acoustic_biomarkers" in result
    assert "evidential_dempster_shafer" in result
    assert "control_plane_micronets" in result
    assert result["control_plane_micronets"]["escalate_to_heavy_verifier"] is True


if __name__ == "__main__":
    test_c11_lpc4_glottal_tkeo_and_russian_closures()
    test_w2_cross_lingual_adaptation_prevents_false_positive_on_russian_norm()
    test_c11_dempster_shafer_dissimulation_detection()
    test_c11_w8a8_micronets_conformal_coverage_and_submillisecond_latency()
    test_end_to_end_hybrid_bridge()
    print("[OK] Все 5 C11-тестов успешно пройдены!")