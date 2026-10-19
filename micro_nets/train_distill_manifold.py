# -*- coding: utf-8 -*-
"""
micro_nets/train_distill_manifold.py
====================================
Обучение и PK-информированная дистилляция многообразия для управляющих микросетей
COMPASS-HYBRID v2.6 с асимметричной функцией потерь Вариана (LinEx Loss),
прогоном через C11 W8A8 целочисленный тракт и Mondrian RAPS конформной калибровкой.
"""

from __future__ import annotations

import json
from typing import Dict, Tuple

import numpy as np

from micro_nets.inference_engine import CompassControlPlaneEngine, QuantizedResidualMicroNet

_EPS: float = 1e-8


def compute_linex_loss_and_grad(
    logits: np.ndarray,
    target_indices: np.ndarray,
    asymmetry_a: float = -2.2,
    critical_class_idx: int = 2,
) -> Tuple[float, np.ndarray]:
    """
    Комбинированная функция потерь:
    Кросс-энтропия + асимметричный штраф Вариана (LinEx) за занижение вероятности
    критического класса безопасности:
        L_LinEx(Delta) = (2 / a^2) * (exp(a * Delta) - a * Delta - 1),
    где Delta = p_pred(critical) - y_true(critical).
    """
    n_samples, n_classes = logits.shape
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    exp_z = np.exp(shifted)
    probs = exp_z / (np.sum(exp_z, axis=1, keepdims=True) + _EPS)

    one_hot = np.zeros_like(probs)
    one_hot[np.arange(n_samples), target_indices] = 1.0

    ce_loss = float(-np.mean(np.sum(one_hot * np.log(np.clip(probs, _EPS, 1.0)), axis=1)))
    grad_logits = (probs - one_hot) / float(n_samples)

    delta = probs[:, critical_class_idx] - one_hot[:, critical_class_idx]
    a = float(asymmetry_a)
    exp_a_delta = np.exp(np.clip(a * delta, -15.0, 15.0))
    linex_vals = (2.0 / (a ** 2)) * (exp_a_delta - a * delta - 1.0)
    linex_loss = float(np.mean(linex_vals))

    d_linex_d_prob = (2.0 / a) * (exp_a_delta - 1.0) / float(n_samples)
    p_crit = probs[:, critical_class_idx : critical_class_idx + 1]
    eye_crit = np.zeros((1, n_classes), dtype=np.float64)
    eye_crit[0, critical_class_idx] = 1.0
    d_prob_d_logits = p_crit * (eye_crit - probs)

    grad_logits += 0.45 * (d_linex_d_prob[:, np.newaxis] * d_prob_d_logits)
    total_loss = ce_loss + 0.45 * linex_loss
    return total_loss, grad_logits


def build_clinical_manifold_dataset(
    n_synthetic: int = 900,
    seed: int = 2026,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Формирует 24-мерное обучающее и калибровочное многообразие по законам
    фармакокинетики CYP2D6/3A4 и русскоязычного био-контура.
    """
    rng = np.random.default_rng(seed)
    X = np.zeros((n_synthetic, 24), dtype=np.float64)
    y_router = np.zeros(n_synthetic, dtype=np.int64)
    y_dissim = np.zeros(n_synthetic, dtype=np.int64)
    y_cluster = np.zeros(n_synthetic, dtype=np.int64)

    for i in range(n_synthetic):
        regime = i % 3
        if regime == 0:
            hama = rng.uniform(0.15, 0.48)
            madrs = rng.uniform(0.10, 0.42)
            wellbeing = rng.uniform(0.62, 0.95)
            cyp2d6 = rng.uniform(1.0, 2.0)
            cyp3a4_inh = rng.uniform(0.9, 1.15)
            egfr = rng.uniform(0.75, 1.1)
            alt_ast = rng.uniform(0.2, 0.65)
            qtc = rng.uniform(0.0, 0.25)
            maoi_inv = 0.0
            tkeo = rng.uniform(0.08, 0.22)
            jitter = rng.uniform(0.25, 0.58)
            shimmer = rng.uniform(1.4, 2.6)
            weibull_k = rng.uniform(1.05, 1.55)
            weibull_lam = rng.uniform(210.0, 380.0) / 1000.0
            pause_frac = rng.uniform(0.10, 0.24)
            conflict_k = rng.uniform(0.02, 0.20)
            dissim_idx = rng.uniform(0.01, 0.16)
            latency = rng.uniform(0.9, 2.2) / 5.0
            mahal_dist = rng.uniform(0.15, 0.48)
            hard_stop = 0.0
            y_router[i], y_dissim[i], y_cluster[i] = 0, 0, 0

        elif regime == 1:
            hama = rng.uniform(0.45, 0.78)
            madrs = rng.uniform(0.40, 0.75)
            wellbeing = rng.uniform(0.35, 0.68)
            cyp2d6 = rng.uniform(0.45, 1.1)
            cyp3a4_inh = rng.uniform(1.15, 1.65)
            egfr = rng.uniform(0.45, 0.78)
            alt_ast = rng.uniform(0.55, 1.15)
            qtc = rng.uniform(0.22, 0.55)
            maoi_inv = float(rng.choice([0.0, 0.25]))
            tkeo = rng.uniform(0.20, 0.38)
            jitter = rng.uniform(0.55, 0.95)
            shimmer = rng.uniform(2.5, 4.2)
            weibull_k = rng.uniform(0.78, 1.08)
            weibull_lam = rng.uniform(360.0, 620.0) / 1000.0
            pause_frac = rng.uniform(0.22, 0.36)
            conflict_k = rng.uniform(0.20, 0.44)
            dissim_idx = rng.uniform(0.16, 0.36)
            latency = rng.uniform(2.1, 3.6) / 5.0
            mahal_dist = rng.uniform(0.46, 0.78)
            hard_stop = 0.0
            y_router[i], y_dissim[i], y_cluster[i] = 1, 1, 1

        else:
            hama = rng.uniform(0.20, 1.15)
            madrs = rng.uniform(0.20, 1.15)
            wellbeing = rng.uniform(0.72, 0.98)
            cyp2d6 = rng.uniform(0.0, 0.45)
            cyp3a4_inh = rng.uniform(1.65, 2.80)
            egfr = rng.uniform(0.15, 0.48)
            alt_ast = rng.uniform(1.15, 2.50)
            qtc = rng.uniform(0.55, 1.20)
            maoi_inv = rng.uniform(0.5, 1.0)
            tkeo = rng.uniform(0.38, 0.85)
            jitter = rng.uniform(0.95, 2.40)
            shimmer = rng.uniform(4.1, 8.5)
            weibull_k = rng.uniform(0.35, 0.78)
            weibull_lam = rng.uniform(620.0, 1450.0) / 1000.0
            pause_frac = rng.uniform(0.35, 0.62)
            conflict_k = rng.uniform(0.45, 0.92)
            dissim_idx = rng.uniform(0.38, 0.90)
            latency = rng.uniform(3.5, 7.5) / 5.0
            mahal_dist = rng.uniform(0.78, 1.65)
            hard_stop = float(rng.choice([0.0, 1.0], p=[0.25, 0.75]))
            y_router[i], y_dissim[i], y_cluster[i] = 2, 2, 2

        X[i] = np.array([
            hama, madrs, 0.5 * (hama + madrs), wellbeing,
            cyp2d6, cyp3a4_inh, egfr, alt_ast,
            qtc, maoi_inv, rng.uniform(0.2, 0.85), rng.uniform(0.0, 1.0),
            tkeo, jitter, shimmer, weibull_k,
            weibull_lam, pause_frac, conflict_k, dissim_idx,
            latency, rng.uniform(-0.5, 0.5), mahal_dist, hard_stop,
        ], dtype=np.float64)

    perm = rng.permutation(n_synthetic)
    return X[perm], y_router[perm], y_dissim[perm], y_cluster[perm]


def train_and_calibrate_micronet(
    net: QuantizedResidualMicroNet,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_cal: np.ndarray,
    y_cal: np.ndarray,
    epochs: int = 90,
    lr: float = 0.08,
    l2_reg: float = 1e-4,
) -> Dict:
    """
    Обучает выходную голову микросети напрямую поверх C11 W8A8 скрытых представлений
    с асимметричным лоссом Вариана (LinEx), квантует веса в INT8 и проводит
    Mondrian RAPS калибровку на X_cal, y_cal.
    """
    input_mean = np.mean(X_train, axis=0).astype(np.float32)
    input_std = np.maximum(np.std(X_train, axis=0), 1e-5).astype(np.float32)
    net.input_mean = input_mean
    net.input_std = input_std
    net.sync_c_descriptor()

    H_train = net.forward_hidden_batch_via_c11(X_train).astype(np.float64)
    w_head = np.zeros((net.output_dim, net.hidden_dim), dtype=np.float64)
    b_head = np.zeros(net.output_dim, dtype=np.float64)

    for ep in range(epochs):
        logits = H_train @ w_head.T + b_head
        _, grad_logits = compute_linex_loss_and_grad(
            logits, y_train, asymmetry_a=-2.4, critical_class_idx=2
        )
        grad_w = grad_logits.T @ H_train + l2_reg * w_head
        grad_b = np.sum(grad_logits, axis=0)

        cur_lr = lr * (0.5 * (1.0 + np.cos(np.pi * ep / float(epochs))))
        w_head -= cur_lr * grad_w
        b_head -= cur_lr * grad_b

    assert net.stem_layer is not None
    w_stem_fp32 = net.stem_layer.weight_int8.astype(np.float64) * net.stem_layer.scale_per_channel[:, np.newaxis]
    b_stem_fp32 = net.stem_layer.bias_fp32.astype(np.float64)

    net.update_head_from_distilled_fp32(
        w_stem=w_stem_fp32,
        b_stem=b_stem_fp32,
        w_head=w_head,
        b_head=b_head,
        input_mean=input_mean,
        input_std=input_std,
    )

    cal_logits = net.forward_logits(X_cal)
    cal_stats = net.conformal_controller.fit_calibration(cal_logits, y_cal, tune_temperature=True)
    net.sync_c_descriptor()

    train_preds = np.argmax(net.forward_logits(X_train), axis=1)
    cal_preds = np.argmax(cal_logits, axis=1)
    return {
        "network": net.name,
        "train_acc": round(float(np.mean(train_preds == y_train)), 4),
        "cal_acc": round(float(np.mean(cal_preds == y_cal)), 4),
        "conformal": cal_stats,
    }


def run_full_control_plane_distillation() -> Dict:
    """
    Запускает полный цикл обучения, C11 W8A8-квантования, конформной калибровки
    и сохранения артефактов всех трех микросетей.
    """
    X, y_router, y_dissim, y_cluster = build_clinical_manifold_dataset(n_synthetic=900, seed=2026)
    n_train = 600
    X_train, X_cal = X[:n_train], X[n_train:]

    engine = CompassControlPlaneEngine()
    stats_router = train_and_calibrate_micronet(
        engine.router_net, X_train, y_router[:n_train], X_cal, y_router[n_train:]
    )
    stats_dissim = train_and_calibrate_micronet(
        engine.dissimulation_net, X_train, y_dissim[:n_train], X_cal, y_dissim[n_train:]
    )
    stats_cluster = train_and_calibrate_micronet(
        engine.cluster_verify_net, X_train, y_cluster[:n_train], X_cal, y_cluster[n_train:]
    )

    saved_path = engine.save_artifacts()
    return {
        "artifacts_meta_path": str(saved_path),
        "router_net": stats_router,
        "dissimulation_net": stats_dissim,
        "cluster_verify_net": stats_cluster,
    }


if __name__ == "__main__":
    report = run_full_control_plane_distillation()
    print(json.dumps(report, ensure_ascii=False, indent=2))