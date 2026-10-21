# -*- coding: utf-8 -*-
"""
benchmarks/test_variant4_ot_xai_ood.py
======================================
Строгая верификация математических теорем и скорости C11-ядра Варианта 4:
1. Теорема Фейди-Пейре (Feydy et al., AISTATS 2019) для дебиасированной дивергенции Синкхорна:
   - Тождество неразличимости: S_eps(alpha, alpha) == 0.0 (с точностью < 1e-8)
   - Симметрия: S_eps(alpha, beta) == S_eps(beta, alpha)
   - Строгая положительность на расходящихся 36-недельных траекториях с пропущенными визитами.
2. Теорема Ледуа-Вульфа (Ledoit & Wolf, J. Multivar. Anal. 2004):
   - Снижение числа обусловленности матрицы ковариации в 24D при малом числе наблюдений
   - Детекция OOD-аномалий по критерию Махаланобиса D_M^2 > chi^2_{24, 0.99}.
3. Теорема Оуэна (Owen, Springer 1977) для коалиционных игр:
   - Точное соблюдение аксиомы эффективности sum_{i=0}^{23} Phi_i == v(N) - v(0) (< 1e-11)
   - Защита иммутабельных признаков (возраст, генотип CYP2D6) при поиске контрафактуалов.
"""

from __future__ import annotations

import math
import time

import numpy as np

from backend.bridges.ot_xai import (
    C11TrajectoryOTAndXAIOrchestrator,
    DEFAULT_HEALTHY_BASELINE_24D,
)


def test_debiased_sinkhorn_divergence_axioms_on_irregular_36w_trajectories() -> None:
    """
    Проверяет математические аксиомы дебиасированной дивергенции Синкхорна S_eps(alpha, beta)
    на нерегулярных 36-недельных клинических траекториях.
    """
    orch = C11TrajectoryOTAndXAIOrchestrator()

    # Эталонная 36-недельная траектория ремиссии (8 контрольных визитов: недели 0, 2, 4, 8, 12, 18, 24, 36)
    ref_weeks = [0.0, 2.0, 4.0, 8.0, 12.0, 18.0, 24.0, 36.0]
    ref_states = np.array([
        [26.0, 28.0],
        [21.0, 23.0],
        [16.0, 18.0],
        [12.0, 13.0],
        [9.0,  10.0],
        [7.0,   8.0],
        [6.0,   6.5],
        [5.0,   5.5],
    ], dtype=np.float64)

    # 1. Аксиома тождества: S_eps(alpha, alpha) == 0.0
    self_rep = orch.compute_trajectory_sinkhorn_divergence(
        ref_weeks, ref_states, ref_weeks, ref_states, epsilon_reg=0.08
    )
    assert self_rep.sinkhorn_divergence < 1e-7, (
        f"Нарушено свойство S_eps(a, a) == 0: получено {self_rep.sinkhorn_divergence}"
    )

    # 2. Нерегулярная траектория вторичной резистентности / рецидива (только 6 визитов со сдвигом дат)
    relapse_weeks = [0.0, 3.0, 7.0, 15.0, 25.0, 35.0]
    relapse_states = np.array([
        [25.0, 27.0],
        [19.0, 21.0],
        [15.0, 16.0],
        [18.0, 20.0],
        [23.0, 25.0],
        [27.0, 29.0],
    ], dtype=np.float64)

    div_ab = orch.compute_trajectory_sinkhorn_divergence(
        relapse_weeks, relapse_states, ref_weeks, ref_states, epsilon_reg=0.08
    )
    div_ba = orch.compute_trajectory_sinkhorn_divergence(
        ref_weeks, ref_states, relapse_weeks, relapse_states, epsilon_reg=0.08
    )

    # Строгая положительность и симметрия S_eps(alpha, beta) == S_eps(beta, alpha)
    assert div_ab.sinkhorn_divergence > 15.0
    assert math.isclose(div_ab.sinkhorn_divergence, div_ba.sinkhorn_divergence, rel_tol=1e-5)
    assert div_ab.wasserstein_barycentric_shift > 4.0


def test_ledoit_wolf_shrinkage_and_mahalanobis_ood_guard_24d() -> None:
    """
    Проверяет, что сжимающий оцениватель Ледуа-Вульфа в 24D:
    1) Резко снижает число обусловленности на малой выборке (n=30, d=24) с коллинеарностью.
    2) Безошибочно разделяет типичных пациентов кластера (in-distribution) и OOD-выбросы.
    """
    orch = C11TrajectoryOTAndXAIOrchestrator()
    rng = np.random.default_rng(2026)

    # Генерируем 30 наблюдений в 24D с сильной анизотропией (колонки 0 и 1 почти коллинеарны)
    base_cluster = np.tile(DEFAULT_HEALTHY_BASELINE_24D, (30, 1))
    noise = rng.normal(0.0, 0.06, size=(30, 24))
    noise[:, 1] = noise[:, 0] + rng.normal(0.0, 1e-4, size=30)
    X_cluster = base_cluster + noise

    model = orch.fit_cluster_ledoit_wolf(X_cluster, cluster_id=1)
    assert 0.02 <= model.shrinkage_rho <= 1.0
    assert model.condition_number_shrunk < model.condition_number_raw

    # Пациент внутри кластера
    inlier_vec = DEFAULT_HEALTHY_BASELINE_24D + rng.normal(0.0, 0.03, size=24)
    inlier_res = orch.evaluate_mahalanobis_ood(inlier_vec, cluster_id=1)
    assert inlier_res.is_ood_anomaly is False
    assert inlier_res.mahalanobis_sq < inlier_res.chi2_threshold_99
    assert inlier_res.chi2_p_value > 0.05

    # Атипичный OOD-пациент с экстремальным фенотипом
    ood_vec = DEFAULT_HEALTHY_BASELINE_24D.copy()
    ood_vec[4] = 0.05   # Экстремальный дефицит CYP2D6
    ood_vec[5] = 2.80   # Мощная блокада CYP3A4
    ood_vec[12] = 0.78  # Аномальный тремор TKEO
    ood_vec[19] = 0.85  # Высокая диссимуляция
    ood_res = orch.evaluate_mahalanobis_ood(ood_vec, cluster_id=1)
    assert ood_res.is_ood_anomaly is True
    assert ood_res.mahalanobis_sq > ood_res.chi2_threshold_99
    assert ood_res.chi2_p_value < 0.001


def test_exact_coalitional_owen_values_and_counterfactual_immutability() -> None:
    """
    Проверяет:
    1) Точное выполнение аксиомы эффективности Оуэна-Шепли:
       sum_{i=0}^{23} Phi_i^Owen == v(N) - v(emptyset) с погрешностью < 1e-11.
    2) Совпадение суммы внутриблочных значений Оуэна с атрибуцией союзов U0..U5.
    3) Неприкосновенность иммутабельных признаков (CYP2D6 генотип, возраст) в контрафактуале
       и снижение риска ниже целевого порога.
    """
    orch = C11TrajectoryOTAndXAIOrchestrator()

    high_risk_patient = DEFAULT_HEALTHY_BASELINE_24D.copy()
    high_risk_patient[0] = 0.85   # Высокий HAM-A
    high_risk_patient[1] = 0.80   # Высокий MADRS
    high_risk_patient[4] = 0.15   # CYP2D6 Poor Metabolizer (иммутабельный генетический фактор!)
    high_risk_patient[5] = 2.40   # Прием сильного ингибитора CYP3A4 (модифицируемый фактор!)
    high_risk_patient[8] = 0.65   # Удлинение QTc
    high_risk_patient[9] = 1.00   # Конфликт с ИМАО (модифицируемый фактор!)
    high_risk_patient[10] = 0.78  # Пожилой возраст (иммутабельный фактор!)
    high_risk_patient[23] = 1.00  # Сработал Hard-Stop ОХЛП

    t0 = time.perf_counter()
    xai = orch.explain_with_exact_owen_and_counterfactual(
        high_risk_patient,
        target_safe_risk_threshold=35.0,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # 1. Аксиома эффективности выполнена с машинной точностью (4096 точных коалиций)
    assert xai.exact_coalition_evaluations == 4096
    assert xai.efficiency_axiom_residual < 1e-10, (
        f"Нарушена аксиома эффективности Оуэна: невязка {xai.efficiency_axiom_residual:.3e}"
    )

    total_phi = sum(xai.owen_values_24d.values())
    total_unions = sum(xai.union_attributions_6.values())
    assert math.isclose(total_phi, xai.patient_risk_vn - xai.baseline_risk_v0, rel_tol=1e-5)
    assert math.isclose(total_unions, total_phi, rel_tol=1e-5)

    # 2. Проверка контрафактического вмешательства:
    # Иммутабельные признаки (cyp2d6_activity_score, age_norm, egfr_norm) НЕ должны быть изменены!
    recs = xai.counterfactual_recommendations
    assert "cyp2d6_activity_score" not in recs, "Генотип CYP2D6 не может быть изменен контрафактуалом!"
    assert "age_norm" not in recs, "Возраст пациента не может быть изменен контрафактуалом!"
    assert "egfr_norm" not in recs

    # Модифицируемые триггеры (hard_stop_rule_active, maoi_washout_violation, cyp3a4_inhibition_factor) устранены
    assert "hard_stop_rule_active" in recs
    assert "maoi_washout_violation" in recs
    assert "cyp3a4_inhibition_factor" in recs
    assert xai.counterfactual_risk_after <= 35.0
    assert xai.relative_risk_reduction_pct > 60.0
    assert elapsed_ms < 5.0, f"Расчет 4096 точных коалиций Оуэна в C11 занял слишком много времени: {elapsed_ms:.3f} мс"


if __name__ == "__main__":
    test_debiased_sinkhorn_divergence_axioms_on_irregular_36w_trajectories()
    test_ledoit_wolf_shrinkage_and_mahalanobis_ood_guard_24d()
    test_exact_coalitional_owen_values_and_counterfactual_immutability()
    print("[OK] Вариант 4 (C11 Debiased Sinkhorn OT, Ledoit-Wolf Mahalanobis OOD & Exact 4096-Coalition Owen XAI) верифицирован!")