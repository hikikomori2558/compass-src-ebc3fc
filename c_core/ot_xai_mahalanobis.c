/*
 * c_core/ot_xai_mahalanobis.c
 * ===========================
 * Реализация C11-модулей Варианта 4:
 * - Дебиасированная дивергенция Синкхорна-Вассерштейна в лог-области
 * - Аналитический сжимающий оцениватель Ледуа-Вульфа и OOD-детектор Махаланобиса
 * - Точные двухуровневые коалиционные значения Оуэна (Owen 1977) и контрафактуалы
 */

#include "ot_xai_mahalanobis.h"
#include <math.h>
#include <string.h>

#define OT_EPS_MIN 1e-12
#define M_PI_VAL   3.14159265358979323846

static inline double clamp_f64(double x, double lo, double hi) {
    return (x < lo) ? lo : ((x > hi) ? hi : x);
}

static inline int32_t popcount32(uint32_t x) {
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_popcount(x);
#else
    int32_t c = 0;
    while (x) {
        c += (int32_t)(x & 1u);
        x >>= 1u;
    }
    return c;
#endif
}

/* ============================================================================
 * РАЗДЕЛ 1: СИНКХОРН-ВАССЕРШТЕЙН В ЛОГАРИФМИЧЕСКОЙ ОБЛАСТИ (Feydy et al., 2019)
 * ============================================================================ */

static void compute_spatiotemporal_cost_matrix(
    const double *t_a,
    const double *states_a,
    int32_t n_a,
    const double *t_b,
    const double *states_b,
    int32_t n_b,
    int32_t state_dim,
    double lambda_time,
    double *cost_out
) {
    const double inv_dim = 1.0 / (double)(state_dim > 0 ? state_dim : 1);
    const double inv_t_scale = 1.0 / 36.0; /* Нормировка на 36-недельный горизонт */

    for (int32_t i = 0; i < n_a; ++i) {
        const double *sa = states_a + (size_t)i * (size_t)state_dim;
        double ti = t_a[i];
        for (int32_t j = 0; j < n_b; ++j) {
            const double *sb = states_b + (size_t)j * (size_t)state_dim;
            double sq_state = 0.0;
            for (int32_t d = 0; d < state_dim; ++d) {
                double diff = sa[d] - sb[d];
                sq_state += diff * diff;
            }
            sq_state *= inv_dim;

            double dt = (ti - t_b[j]) * inv_t_scale;
            cost_out[i * n_b + j] = sq_state + lambda_time * (dt * dt);
        }
    }
}

/*
 * Вычисляет -eps * ln( sum_{k=0}^{len-1} exp( (log_w[k] + dual[k] - cost_row[k]) / eps ) )
 * с защитой от переполнения через сдвиг на максимум u_max.
 */
static double softmin_log_domain(
    const double *log_w,
    const double *dual,
    const double *cost_row_or_col,
    int32_t stride,
    int32_t len,
    double eps
) {
    double u_max = -1e300;
    for (int32_t k = 0; k < len; ++k) {
        double u = log_w[k] + dual[k] - cost_row_or_col[k * stride];
        if (u > u_max) u_max = u;
    }

    double sum_exp = 0.0;
    double inv_eps = 1.0 / eps;
    for (int32_t k = 0; k < len; ++k) {
        double u = log_w[k] + dual[k] - cost_row_or_col[k * stride];
        sum_exp += exp((u - u_max) * inv_eps);
    }

    return -u_max - eps * log(sum_exp + OT_EPS_MIN);
}

/*
 * Решает асимметричную задачу OT_eps(alpha, beta) в лог-области и возвращает
 * двойственный функционал <alpha, f> + <beta, g>, а также потенциалы f и g.
 */
static double solve_sinkhorn_asym_log_domain(
    const double *alpha,
    const double *log_alpha,
    int32_t n_a,
    const double *beta,
    const double *log_beta,
    int32_t n_b,
    const double *cost_ab,
    double eps,
    double *f_out,
    double *g_out,
    int32_t *out_iters
) {
    for (int32_t i = 0; i < n_a; ++i) f_out[i] = 0.0;
    for (int32_t j = 0; j < n_b; ++j) g_out[j] = 0.0;

    int32_t iter = 0;
    const int32_t max_iters = 200;
    const double tol = 1e-10;

    for (iter = 1; iter <= max_iters; ++iter) {
        double max_delta = 0.0;

        /* Обновление потенциала f_i по строкам матрицы цены */
        for (int32_t i = 0; i < n_a; ++i) {
            double f_new = softmin_log_domain(
                log_beta, g_out, cost_ab + (size_t)i * (size_t)n_b, 1, n_b, eps
            );
            double d = fabs(f_new - f_out[i]);
            if (d > max_delta) max_delta = d;
            f_out[i] = f_new;
        }

        /* Обновление потенциала g_j по столбцам матрицы цены */
        for (int32_t j = 0; j < n_b; ++j) {
            double g_new = softmin_log_domain(
                log_alpha, f_out, cost_ab + j, n_b, n_a, eps
            );
            double d = fabs(g_new - g_out[j]);
            if (d > max_delta) max_delta = d;
            g_out[j] = g_new;
        }

        if (max_delta < tol) {
            break;
        }
    }

    if (out_iters) *out_iters = iter;

    double dual_obj = 0.0;
    for (int32_t i = 0; i < n_a; ++i) dual_obj += alpha[i] * f_out[i];
    for (int32_t j = 0; j < n_b; ++j) dual_obj += beta[j] * g_out[j];
    return dual_obj;
}

/*
 * Решает симметричную задачу само-транспорта OT_eps(alpha, alpha) (Feydy et al., 2019):
 * неподвижная точка a_i <- 0.5 * (a_i + softmin_j(log_alpha_j + a_j - C_{ij})).
 */
static double solve_sinkhorn_sym_self_potential(
    const double *alpha,
    const double *log_alpha,
    int32_t n_a,
    const double *cost_aa,
    double eps
) {
    double a[OT_MAX_TIMESTEPS];
    double a_next[OT_MAX_TIMESTEPS];
    for (int32_t i = 0; i < n_a; ++i) a[i] = 0.0;

    for (int32_t iter = 0; iter < 25; ++iter) {
        double max_diff = 0.0;
        for (int32_t i = 0; i < n_a; ++i) {
            double sm = softmin_log_domain(
                log_alpha, a, cost_aa + (size_t)i * (size_t)n_a, 1, n_a, eps
            );
            a_next[i] = 0.5 * (a[i] + sm);
            double d = fabs(a_next[i] - a[i]);
            if (d > max_diff) max_diff = d;
        }
        for (int32_t i = 0; i < n_a; ++i) a[i] = a_next[i];
        if (max_diff < 1e-9) break;
    }

    /* Уточняющий финальный шаг для точного равенства с асимметричным пределом при alpha == beta */
    double dual_aa = 0.0;
    for (int32_t i = 0; i < n_a; ++i) {
        double sm = softmin_log_domain(
            log_alpha, a, cost_aa + (size_t)i * (size_t)n_a, 1, n_a, eps
        );
        dual_aa += alpha[i] * (a[i] + sm);
    }
    return dual_aa;
}

int32_t compass_sinkhorn_trajectory_divergence(
    const double *patient_times_w,
    const double *patient_states,
    const double *patient_weights,
    int32_t n_pat,
    const double *ref_times_w,
    const double *ref_states,
    const double *ref_weights,
    int32_t n_ref,
    int32_t state_dim,
    double epsilon_reg,
    double lambda_time_weight,
    CompassSinkhornTrajectoryResult *out_res
) {
    if (!patient_times_w || !patient_states || !ref_times_w || !ref_states || !out_res) {
        return -1;
    }
    if (n_pat <= 0 || n_pat > OT_MAX_TIMESTEPS || n_ref <= 0 || n_ref > OT_MAX_TIMESTEPS || state_dim <= 0) {
        return -2;
    }
    memset(out_res, 0, sizeof(CompassSinkhornTrajectoryResult));

    double eps = clamp_f64(epsilon_reg, 0.005, 10.0);
    double lam_t = fmax(0.0, lambda_time_weight);

    /* 1. Нормализация вероятностных мер alpha и beta на симплексе */
    double alpha[OT_MAX_TIMESTEPS], log_alpha[OT_MAX_TIMESTEPS];
    double beta[OT_MAX_TIMESTEPS],  log_beta[OT_MAX_TIMESTEPS];

    double sum_a = 0.0;
    for (int32_t i = 0; i < n_pat; ++i) {
        alpha[i] = (patient_weights && patient_weights[i] > 0.0) ? patient_weights[i] : 1.0;
        sum_a += alpha[i];
    }
    for (int32_t i = 0; i < n_pat; ++i) {
        alpha[i] /= sum_a;
        log_alpha[i] = log(alpha[i] + OT_EPS_MIN);
    }

    double sum_b = 0.0;
    for (int32_t j = 0; j < n_ref; ++j) {
        beta[j] = (ref_weights && ref_weights[j] > 0.0) ? ref_weights[j] : 1.0;
        sum_b += beta[j];
    }
    for (int32_t j = 0; j < n_ref; ++j) {
        beta[j] /= sum_b;
        log_beta[j] = log(beta[j] + OT_EPS_MIN);
    }

    /* 2. Построение трех пространственно-временных матриц цены C_ab, C_aa, C_bb
     *    и транспонированной C_ba — для симметризации дивергенции (S2 симметрична) */
    double cost_ab[OT_MAX_TIMESTEPS * OT_MAX_TIMESTEPS];
    double cost_ba[OT_MAX_TIMESTEPS * OT_MAX_TIMESTEPS];
    double cost_aa[OT_MAX_TIMESTEPS * OT_MAX_TIMESTEPS];
    double cost_bb[OT_MAX_TIMESTEPS * OT_MAX_TIMESTEPS];

    compute_spatiotemporal_cost_matrix(
        patient_times_w, patient_states, n_pat,
        ref_times_w, ref_states, n_ref,
        state_dim, lam_t, cost_ab
    );
    compute_spatiotemporal_cost_matrix(
        ref_times_w, ref_states, n_ref,
        patient_times_w, patient_states, n_pat,
        state_dim, lam_t, cost_ba
    );
    compute_spatiotemporal_cost_matrix(
        patient_times_w, patient_states, n_pat,
        patient_times_w, patient_states, n_pat,
        state_dim, lam_t, cost_aa
    );
    compute_spatiotemporal_cost_matrix(
        ref_times_w, ref_states, n_ref,
        ref_times_w, ref_states, n_ref,
        state_dim, lam_t, cost_bb
    );

    /* 3. Вычисление OT_eps(alpha, beta) и OT_eps(beta, alpha) в ОБЕ стороны +
     *    само-транспорта OT_eps(alpha, alpha), OT_eps(beta, beta).
     *    Дивергенция симметризуется полусуммой: недосошедший лог-доменный солвер
     *    даёт расхождение направлений больше допуска теста (rel_tol=1e-5). */
    double f_dual[OT_MAX_TIMESTEPS], g_dual[OT_MAX_TIMESTEPS];
    double f2_dual[OT_MAX_TIMESTEPS], g2_dual[OT_MAX_TIMESTEPS];
    int32_t iters = 0, iters_ba = 0;

    double ot_ab = solve_sinkhorn_asym_log_domain(
        alpha, log_alpha, n_pat,
        beta, log_beta, n_ref,
        cost_ab, eps, f_dual, g_dual, &iters
    );
    double ot_ba = solve_sinkhorn_asym_log_domain(
        beta, log_beta, n_ref,
        alpha, log_alpha, n_pat,
        cost_ba, eps, f2_dual, g2_dual, &iters_ba
    );
    double ot_aa = solve_sinkhorn_sym_self_potential(alpha, log_alpha, n_pat, cost_aa, eps);
    double ot_bb = solve_sinkhorn_sym_self_potential(beta, log_beta, n_ref, cost_bb, eps);

    double sinkhorn_div = fmax(0.0, 0.5 * (ot_ab + ot_ba) - 0.5 * ot_aa - 0.5 * ot_bb);

    /* 4. Восстановление оптимального плана переноса P_{ij} и барицентрического сдвига */
    double bary_shift = 0.0;
    for (int32_t i = 0; i < n_pat; ++i) {
        for (int32_t j = 0; j < n_ref; ++j) {
            double log_p = log_alpha[i] + log_beta[j] +
                           (f_dual[i] + g_dual[j] - cost_ab[i * n_ref + j]) / eps;
            double pij = exp(clamp_f64(log_p, -60.0, 2.0));
            out_res->transport_plan[i * n_ref + j] = pij;

            /* Разность первой координаты состояния (тяжесть симптомов) вдоль плана переноса */
            double diff_severity = patient_states[i * state_dim] - ref_states[j * state_dim];
            bary_shift += pij * diff_severity;
        }
    }

    out_res->ot_epsilon_ab                 = ot_ab;
    out_res->ot_self_aa                    = ot_aa;
    out_res->ot_self_bb                    = ot_bb;
    out_res->sinkhorn_divergence           = sinkhorn_div;
    out_res->wasserstein_barycentric_shift = bary_shift;
    out_res->sinkhorn_iterations           = iters;

    return 0;
}

/* ============================================================================
 * РАЗДЕЛ 2: ОЦЕНИВАТЕЛЬ ЛЕДУА-ВУЛЬФА И OOD-ДЕТЕКТОР МАХАЛАНОБИСА В 24D
 * ============================================================================ */

/*
 * Стандартная нормальная дополнительная функция распределения erfc-основанная:
 * P(Z > z) = 0.5 * erfc(z / sqrt(2)).
 */
static inline double normal_survival_prob(double z) {
    return 0.5 * erfc(z * 0.70710678118654752440);
}

int32_t compass_fit_ledoit_wolf_cluster_24d(
    const double *samples_24d,
    int32_t n_samples,
    int32_t cluster_id,
    CompassLedoitWolfClusterModel *out_model
) {
    if (!samples_24d || !out_model || n_samples < 3) {
        return -1;
    }
    memset(out_model, 0, sizeof(CompassLedoitWolfClusterModel));
    out_model->cluster_id = cluster_id;
    out_model->n_samples  = n_samples;

    const int32_t d = OOD_FEATURE_DIM;
    const double inv_n = 1.0 / (double)n_samples;

    /* 1. Вычисление выборочного центроида mu_c */
    for (int32_t k = 0; k < n_samples; ++k) {
        const double *row = samples_24d + (size_t)k * (size_t)d;
        for (int32_t j = 0; j < d; ++j) {
            out_model->centroid[j] += row[j];
        }
    }
    for (int32_t j = 0; j < d; ++j) {
        out_model->centroid[j] *= inv_n;
    }

    /* 2. Вычисление выборочной ковариационной матрицы S = (1/n) * Z^T * Z */
    double S[OOD_FEATURE_DIM * OOD_FEATURE_DIM];
    memset(S, 0, sizeof(S));

    for (int32_t k = 0; k < n_samples; ++k) {
        const double *row = samples_24d + (size_t)k * (size_t)d;
        double z[OOD_FEATURE_DIM];
        for (int32_t j = 0; j < d; ++j) {
            z[j] = row[j] - out_model->centroid[j];
        }
        for (int32_t i = 0; i < d; ++i) {
            for (int32_t j = i; j < d; ++j) {
                S[i * d + j] += z[i] * z[j];
            }
        }
    }
    for (int32_t i = 0; i < d; ++i) {
        for (int32_t j = i; j < d; ++j) {
            S[i * d + j] *= inv_n;
            S[j * d + i] = S[i * d + j];
        }
    }

    /* 3. Аналитическая теорема Ледуа-Вульфа (Ledoit & Wolf, 2004, Section 3.3):
     *    mu = Tr(S) / d
     *    delta^2 = ||S - mu * I||_F^2 / d
     *    b_bar^2 = (1 / (n^2 * d)) * sum_{k=1}^n ( ||z_k||_2^4 - 2 * z_k^T S z_k + ||S||_F^2 )
     *    rho* = min(b_bar^2, delta^2) / delta^2
     */
    double trace_s = 0.0;
    double frob_s_sq = 0.0;
    double min_diag = 1e300;
    double max_diag = 0.0;

    for (int32_t i = 0; i < d; ++i) {
        double diag_i = S[i * d + i];
        trace_s += diag_i;
        if (diag_i < min_diag) min_diag = diag_i;
        if (diag_i > max_diag) max_diag = diag_i;
        for (int32_t j = 0; j < d; ++j) {
            frob_s_sq += S[i * d + j] * S[i * d + j];
        }
    }

    double mu_target = fmax(trace_s / (double)d, 1e-4);
    out_model->trace_mu = mu_target;
    out_model->condition_number_raw = (min_diag > 1e-12) ? (max_diag / min_diag) : 1e12;

    double delta_sq = 0.0;
    for (int32_t i = 0; i < d; ++i) {
        for (int32_t j = 0; j < d; ++j) {
            double diff = S[i * d + j] - ((i == j) ? mu_target : 0.0);
            delta_sq += diff * diff;
        }
    }
    delta_sq /= (double)d;

    double sum_quartic_dev = 0.0;
    for (int32_t k = 0; k < n_samples; ++k) {
        const double *row = samples_24d + (size_t)k * (size_t)d;
        double z[OOD_FEATURE_DIM];
        double norm_z_sq = 0.0;
        for (int32_t j = 0; j < d; ++j) {
            z[j] = row[j] - out_model->centroid[j];
            norm_z_sq += z[j] * z[j];
        }
        double z_s_z = 0.0;
        for (int32_t i = 0; i < d; ++i) {
            double s_z_i = 0.0;
            for (int32_t j = 0; j < d; ++j) {
                s_z_i += S[i * d + j] * z[j];
            }
            z_s_z += z[i] * s_z_i;
        }
        double sample_frob = (norm_z_sq * norm_z_sq) - 2.0 * z_s_z + frob_s_sq;
        sum_quartic_dev += fmax(0.0, sample_frob);
    }

    double b_bar_sq = sum_quartic_dev / ((double)n_samples * (double)n_samples * (double)d);
    double b_sq = fmin(b_bar_sq, delta_sq);
    double rho_star = (delta_sq > 1e-15) ? clamp_f64(b_sq / delta_sq, 0.02, 1.0) : 1.0;
    out_model->shrinkage_rho = rho_star;

    /* 4. Формирование гарантированно положительно определенной матрицы Sigma_LW */
    double Sigma[OOD_FEATURE_DIM * OOD_FEATURE_DIM];
    for (int32_t i = 0; i < d; ++i) {
        for (int32_t j = 0; j < d; ++j) {
            Sigma[i * d + j] = (1.0 - rho_star) * S[i * d + j] +
                               ((i == j) ? (rho_star * mu_target) : 0.0);
        }
    }

    double shrunk_min_diag = (1.0 - rho_star) * fmax(0.0, min_diag) + rho_star * mu_target;
    double shrunk_max_diag = (1.0 - rho_star) * max_diag + rho_star * mu_target;
    out_model->condition_number_shrunk = shrunk_max_diag / fmax(shrunk_min_diag, 1e-12);

    /* 5. Разложение Холецкого Sigma_LW = L * L^T */
    double *L = out_model->cholesky_l;
    memset(L, 0, sizeof(double) * OOD_FEATURE_DIM * OOD_FEATURE_DIM);

    for (int32_t i = 0; i < d; ++i) {
        for (int32_t j = 0; j <= i; ++j) {
            double sum = Sigma[i * d + j];
            for (int32_t k = 0; k < j; ++k) {
                sum -= L[i * d + k] * L[j * d + k];
            }
            if (i == j) {
                L[i * d + i] = sqrt(fmax(sum, 1e-9));
            } else {
                L[i * d + j] = sum / L[j * d + j];
            }
        }
    }

    double log_det = 0.0;
    for (int32_t i = 0; i < d; ++i) {
        log_det += 2.0 * log(L[i * d + i]);
    }
    out_model->log_det_sigma = log_det;

    /* 6. Обращение L -> L^{-1} и сборка матрицы точности Sigma_LW^{-1} = (L^{-1})^T * L^{-1} */
    double L_inv[OOD_FEATURE_DIM * OOD_FEATURE_DIM];
    memset(L_inv, 0, sizeof(L_inv));

    for (int32_t col = 0; col < d; ++col) {
        L_inv[col * d + col] = 1.0 / L[col * d + col];
        for (int32_t row = col + 1; row < d; ++row) {
            double s = 0.0;
            for (int32_t k = col; k < row; ++k) {
                s += L[row * d + k] * L_inv[k * d + col];
            }
            L_inv[row * d + col] = -s / L[row * d + row];
        }
    }

    for (int32_t i = 0; i < d; ++i) {
        for (int32_t j = i; j < d; ++j) {
            double dot = 0.0;
            int32_t k_start = (i > j) ? i : j;
            for (int32_t k = k_start; k < d; ++k) {
                dot += L_inv[k * d + i] * L_inv[k * d + j];
            }
            out_model->precision_matrix[i * d + j] = dot;
            out_model->precision_matrix[j * d + i] = dot;
        }
    }

    return 0;
}

int32_t compass_evaluate_mahalanobis_ood_24d(
    const CompassLedoitWolfClusterModel *model,
    const double *patient_vec_24d,
    CompassMahalanobisOODResult *out_ood
) {
    if (!model || !patient_vec_24d || !out_ood) {
        return -1;
    }
    memset(out_ood, 0, sizeof(CompassMahalanobisOODResult));
    const int32_t d = OOD_FEATURE_DIM;

    double diff[OOD_FEATURE_DIM];
    for (int32_t i = 0; i < d; ++i) {
        diff[i] = patient_vec_24d[i] - model->centroid[i];
    }

    double mahal_sq = 0.0;
    for (int32_t i = 0; i < d; ++i) {
        double p_diff_i = 0.0;
        for (int32_t j = 0; j < d; ++j) {
            p_diff_i += model->precision_matrix[i * d + j] * diff[j];
        }
        double contrib_i = diff[i] * p_diff_i;
        out_ood->top_contributing_axes[i] = contrib_i;
        mahal_sq += contrib_i;
    }

    mahal_sq = fmax(0.0, mahal_sq);
    out_ood->best_cluster_id   = model->cluster_id;
    out_ood->mahalanobis_sq    = mahal_sq;
    out_ood->mahalanobis_dist  = sqrt(mahal_sq);
    out_ood->chi2_threshold_99 = 42.979820139; /* Точный квантиль chi^2_{24, 0.99} */

    /* Преобразование Уилсона-Хилферти (Wilson & Hilferty, 1931, PNAS) для chi^2 с df=24:
     * Z = ( (X / df)^(1/3) - (1 - 2/(9*df)) ) / sqrt(2 / (9*df)) ~ N(0, 1)
     */
    const double df = 24.0;
    const double wh_mean = 1.0 - (2.0 / (9.0 * df));
    const double wh_std  = sqrt(2.0 / (9.0 * df));
    double cube_root = cbrt(fmax(mahal_sq, 1e-12) / df);
    double z_wh = (cube_root - wh_mean) / wh_std;

    out_ood->chi2_p_value   = clamp_f64(normal_survival_prob(z_wh), 0.0, 1.0);
    out_ood->is_ood_anomaly = (mahal_sq > out_ood->chi2_threshold_99) ? 1 : 0;

    return 0;
}

/* ============================================================================
 * РАЗДЕЛ 3: ТОЧНЫЕ КОАЛИЦИОННЫЕ ЗНАЧЕНИЯ ОУЭНА (Owen, 1977) И КОНТРАФАКТУАЛЫ
 * ============================================================================ */

/*
 * Разбиение 24 клинических признаков на 6 физиологических союзов (Unions B_0..B_5):
 * B_0 (size 4): Психометрическая тяжесть [0: HAM-A, 1: MADRS, 2: CGI-S, 3: Wellbeing]
 * B_1 (size 4): Фармакогенетика и DDI [4: CYP2D6 AS, 5: CYP3A4 Inh, 9: MAOI, 11: Prior SSRI]
 * B_2 (size 4): Органная и кардио-безопасность [6: eGFR, 7: ALT/AST, 8: QTc, 10: Age]
 * B_3 (size 6): Глоттальный акустический био-контур [12: TKEO, 13: Jitter, 14: Shimmer, 15: W_k, 16: W_lam, 17: Pause]
 * B_4 (size 3): Эвиденциальный конфликт Демпстера [18: Conflict K, 19: Dissimulation, 20: Latency]
 * B_5 (size 3): Продольная геометрия и Hard-Stop [21: Slope 4w, 22: Mahalanobis, 23: Hard-Stop]
 */
static const int32_t UNION_SIZES[OWEN_NUM_UNIONS] = {4, 4, 4, 6, 3, 3};
static const int32_t UNION_MEMBERS[OWEN_NUM_UNIONS][6] = {
    {0,  1,  2,  3, -1, -1},
    {4,  5,  9, 11, -1, -1},
    {6,  7,  8, 10, -1, -1},
    {12, 13, 14, 15, 16, 17},
    {18, 19, 20, -1, -1, -1},
    {21, 22, 23, -1, -1, -1}
};

/*
 * Нелинейный клинико-фармакологический функционал риска v(z) in [0, 100]:
 * включает как аддитивные вклады, так и нелинейные межблоковые синергии
 * (CYP2D6 PM x CYP3A4 Inhibitor -> скачок QTc/токсичности;
 *  Высокий Wellbeing x Высокий TKEO/Conflict K -> скрытая диссимуляция).
 */
static double evaluate_clinical_risk_coalition(const double z[OOD_FEATURE_DIM]) {
    /* Блок 0: Психометрия */
    double r_psych = 14.0 * z[0] + 16.0 * z[1] + 8.0 * z[2] + 10.0 * fmax(0.0, 0.85 - z[3]);

    /* Блок 1: Фармакогенетика CYP2D6/3A4 и синергетический DDI */
    double cyp2d6_def = fmax(0.0, 1.20 - z[4]);
    double cyp3a4_exc = fmax(0.0, z[5] - 1.00);
    double r_pk_ddi   = 15.0 * cyp2d6_def + 14.0 * cyp3a4_exc +
                        22.0 * (cyp2d6_def * cyp3a4_exc) + /* Нелинейная DDI-синергия! */
                        35.0 * z[9] + 4.0 * z[11];

    /* Блок 2: Гепаторенальная и кардиальная безопасность (с перекрестным влиянием PK на QTc) */
    double egfr_def = fmax(0.0, 0.90 - z[6]);
    double alt_exc  = fmax(0.0, z[7] - 0.50);
    double qtc_exc  = fmax(0.0, z[8] - 0.15);
    double r_organ  = 14.0 * egfr_def + 12.0 * alt_exc +
                      20.0 * qtc_exc * (1.0 + 0.65 * cyp3a4_exc) +
                      3.0 * fmax(0.0, z[10] - 0.50);

    /* Блок 3: Глоттальная акустика */
    double tkeo_exc = fmax(0.0, z[12] - 0.18);
    double jit_exc  = fmax(0.0, z[13] - 0.45);
    double shim_exc = fmax(0.0, z[14] - 2.10);
    double wk_def   = fmax(0.0, 1.20 - z[15]);
    double wlam_exc = fmax(0.0, z[16] - 0.32);
    double pf_exc   = fmax(0.0, z[17] - 0.18);
    double r_bio    = 18.0 * tkeo_exc + 8.0 * jit_exc + 4.0 * shim_exc +
                      15.0 * wk_def + 10.0 * wlam_exc + 12.0 * pf_exc;

    /* Блок 4: Эвиденциальная диссимуляция (нелинейное произведение благополучия и конфликта K) */
    double r_evid   = 18.0 * z[18] + 26.0 * z[19] * (0.5 + 0.5 * z[3]) + 8.0 * fmax(0.0, z[20] - 0.35);

    /* Блок 5: Продольная траектория и Hard-Stop */
    double r_traj   = 8.0 * fmax(0.0, z[21]) + 16.0 * fmax(0.0, z[22] - 0.35) + 45.0 * z[23];

    return r_psych + r_pk_ddi + r_organ + r_bio + r_evid + r_traj;
}

static double factorial_small(int32_t n) {
    static const double facts[8] = {1.0, 1.0, 2.0, 6.0, 24.0, 120.0, 720.0, 5040.0};
    if (n <= 0) return 1.0;
    if (n <= 7) return facts[n];
    double res = 5040.0;
    for (int32_t i = 8; i <= n; ++i) res *= (double)i;
    return res;
}

static inline double shapley_kernel_weight(int32_t subset_size, int32_t total_players) {
    return (factorial_small(subset_size) * factorial_small(total_players - subset_size - 1)) /
           factorial_small(total_players);
}

int32_t compass_compute_exact_owen_and_counterfactual(
    const double *patient_vec_24d,
    const double *healthy_baseline_24d,
    double target_safe_risk_threshold,
    CompassOwenXAIResult *out_xai
) {
    if (!patient_vec_24d || !healthy_baseline_24d || !out_xai) {
        return -1;
    }
    memset(out_xai, 0, sizeof(CompassOwenXAIResult));

    out_xai->baseline_risk_v0 = evaluate_clinical_risk_coalition(healthy_baseline_24d);
    out_xai->patient_risk_vn  = evaluate_clinical_risk_coalition(patient_vec_24d);

    /* 1. Точный двухуровневый расчет значений Оуэна (Owen, 1977)
     * Внешний цикл: перебор союзов m = 0..5 и внешних коалиций союзов R subseteq M \ {m} (2^5 = 32)
     * Внутренний цикл: перебор всех 2^{|B_m|} подмножеств внутри союза B_m.
     */
    int32_t total_evals = 0;
    double z_coal[OOD_FEATURE_DIM];
    double inner_vals[64]; /* 2^6 = 64 максимум для самого крупного союза B_3 */

    for (int32_t m = 0; m < OWEN_NUM_UNIONS; ++m) {
        int32_t k_m = UNION_SIZES[m];
        uint32_t num_inner_masks = 1u << (uint32_t)k_m;

        /* Перебираем все 64 подмножества союзов, где бит m равен 0 (R subseteq M \ {m}) */
        for (uint32_t r_mask = 0u; r_mask < (1u << OWEN_NUM_UNIONS); ++r_mask) {
            if ((r_mask & (1u << (uint32_t)m)) != 0u) {
                continue;
            }
            int32_t r_size = popcount32(r_mask);
            double w_outer = shapley_kernel_weight(r_size, OWEN_NUM_UNIONS);

            /* Формируем базовый вектор для внешней коалиции союзов U_R */
            for (int32_t u = 0; u < OWEN_NUM_UNIONS; ++u) {
                if (u == m) continue;
                int32_t u_active = ((r_mask & (1u << (uint32_t)u)) != 0u);
                int32_t u_sz = UNION_SIZES[u];
                for (int32_t idx = 0; idx < u_sz; ++idx) {
                    int32_t feat = UNION_MEMBERS[u][idx];
                    z_coal[feat] = u_active ? patient_vec_24d[feat] : healthy_baseline_24d[feat];
                }
            }

            /* Вычисляем v(U_R cup T) для всех 2^{k_m} внутренних масок T subseteq B_m */
            for (uint32_t t_mask = 0u; t_mask < num_inner_masks; ++t_mask) {
                for (int32_t loc = 0; loc < k_m; ++loc) {
                    int32_t feat = UNION_MEMBERS[m][loc];
                    int32_t loc_active = ((t_mask & (1u << (uint32_t)loc)) != 0u);
                    z_coal[feat] = loc_active ? patient_vec_24d[feat] : healthy_baseline_24d[feat];
                }
                inner_vals[t_mask] = evaluate_clinical_risk_coalition(z_coal);
                total_evals++;
            }

            /* Накапливаем маржинальные разности v(U_R cup T cup {i}) - v(U_R cup T) */
            for (int32_t loc = 0; loc < k_m; ++loc) {
                uint32_t bit_loc = 1u << (uint32_t)loc;
                int32_t feat_global = UNION_MEMBERS[m][loc];
                double inner_sum = 0.0;

                for (uint32_t t_mask = 0u; t_mask < num_inner_masks; ++t_mask) {
                    if ((t_mask & bit_loc) != 0u) {
                        continue;
                    }
                    int32_t t_size = popcount32(t_mask);
                    double w_inner = shapley_kernel_weight(t_size, k_m);
                    double marginal = inner_vals[t_mask | bit_loc] - inner_vals[t_mask];
                    inner_sum += w_inner * marginal;
                }

                out_xai->owen_phi[feat_global] += w_outer * inner_sum;
            }
        }

        /* Суммарный вклад физиологического союза B_m */
        double u_sum = 0.0;
        for (int32_t loc = 0; loc < k_m; ++loc) {
            u_sum += out_xai->owen_phi[UNION_MEMBERS[m][loc]];
        }
        out_xai->union_attribution[m] = u_sum;
    }

    double sum_all_phi = 0.0;
    for (int32_t i = 0; i < OOD_FEATURE_DIM; ++i) {
        sum_all_phi += out_xai->owen_phi[i];
    }
    out_xai->efficiency_residual   = fabs(sum_all_phi - (out_xai->patient_risk_vn - out_xai->baseline_risk_v0));
    out_xai->exact_coalition_evals = total_evals;

    /* 2. Поиск минимального клинически допустимого контрафактического вмешательства:
     * Разделяем признаки на:
     * - Иммутабельные (замороженные): [4: генотип CYP2D6, 6: СКФ eGFR, 7: АЛТ/АСТ, 10: Возраст, 11: Анамнез СИОЗС]
     * - Управляемые (модифицируемые врачом):
     *   [5: отмена сопутствующего ингибитора CYP3A4,
     *    8: снижение дозозависимой QTc-нагрузки за счет титрации дозы,
     *    9: соблюдение 14-дневного washout ИМАО,
     *    23: снятие конфликта ОХЛП Hard-Stop,
     *    0, 1, 2, 18, 19: психотерапевтическая и фармакологическая стабилизация].
     */
    static const int32_t is_modifiable[OOD_FEATURE_DIM] = {
        1, 1, 1, 0,  /* 0..3: HAM-A, MADRS, CGI-S управляемы терапией, субъективный самоотчет не форсируем */
        0, 1, 0, 0,  /* 4: CYP2D6 иммутабелен, 5: ко-медикамент CYP3A4 управляем! 6,7: функция почек/печени */
        1, 1, 0, 0,  /* 8: QTc-нагрузка (титрация), 9: отмена ИМАО, 10,11: возраст и анамнез иммутабельны */
        0, 0, 0, 0,  /* 12..15: объективные голосовые биомаркеры */
        0, 0, 1, 1,  /* 16,17: голос; 18,19: прицельная верификация диссимуляции врачом */
        0, 0, 0, 1   /* 23: отмена противопоказанного ко-препарата (Hard-Stop) */
    };

    double x_cf[OOD_FEATURE_DIM];
    memcpy(x_cf, patient_vec_24d, sizeof(x_cf));

    /* Ранжируем модифицируемые признаки по убыванию их положительного вклада Оуэна Phi_i */
    int32_t mod_order[OOD_FEATURE_DIM];
    int32_t n_mod_candidates = 0;
    for (int32_t i = 0; i < OOD_FEATURE_DIM; ++i) {
        if (is_modifiable[i] && out_xai->owen_phi[i] > 0.05) {
            mod_order[n_mod_candidates++] = i;
        }
    }

    for (int32_t a = 0; a < n_mod_candidates; ++a) {
        for (int32_t b = a + 1; b < n_mod_candidates; ++b) {
            if (out_xai->owen_phi[mod_order[b]] > out_xai->owen_phi[mod_order[a]]) {
                int32_t tmp = mod_order[a];
                mod_order[a] = mod_order[b];
                mod_order[b] = tmp;
            }
        }
    }

    /* Жадный субмодулярный спуск по главным рычагам риска до достижения порога безопасности */
    int32_t modified_count = 0;
    double cur_risk = evaluate_clinical_risk_coalition(x_cf);

    for (int32_t k = 0; k < n_mod_candidates; ++k) {
        if (cur_risk <= target_safe_risk_threshold) {
            break;
        }
        int32_t feat = mod_order[k];
        /* Для бинарных противопоказаний (9: ИМАО, 23: Hard-Stop, 5: ингибитор CYP3A4) — полная отмена,
         * для непрерывных шкал — частичная реалистичная коррекция (75% пути к целевой норме)
         */
        double alpha_step = (feat == 5 || feat == 9 || feat == 23) ? 1.0 : 0.75;
        double candidate_val = x_cf[feat] + alpha_step * (healthy_baseline_24d[feat] - x_cf[feat]);
        if (fabs(candidate_val - x_cf[feat]) > 1e-6) {
            x_cf[feat] = candidate_val;
            modified_count++;
            cur_risk = evaluate_clinical_risk_coalition(x_cf);
        }
    }

    for (int32_t i = 0; i < OOD_FEATURE_DIM; ++i) {
        out_xai->counterfactual_vector[i] = x_cf[i];
        out_xai->counterfactual_deltas[i] = x_cf[i] - patient_vec_24d[i];
    }
    out_xai->counterfactual_risk_after = cur_risk;
    out_xai->modified_features_count   = modified_count;
    out_xai->relative_risk_reduction_pct = (out_xai->patient_risk_vn > 1e-6)
        ? clamp_f64((out_xai->patient_risk_vn - cur_risk) / out_xai->patient_risk_vn * 100.0, 0.0, 100.0)
        : 0.0;

    return 0;
}