/*
 * c_core/pk_rk45_solver.c
 * =======================
 * Реализация адаптивного решателя ОДУ Дорманда-Принса RK45(5)4 (DOPRI5)
 * с контролем локальной погрешности, свойством FSAL и проверкой инварианта
 * сохранения массы для нелинейной CYP2D6/CYP3A4 фармакокинетики.
 */

#include "pk_rk45_solver.h"
#include <math.h>
#include <string.h>

#define PK_EPS 1e-12

/*
 * Точные рациональные коэффициенты таблицы Бутчера Дорманда-Принса (Dormand & Prince, 1980)
 */
static const double DP_A21 = 1.0 / 5.0;

static const double DP_A31 = 3.0 / 40.0;
static const double DP_A32 = 9.0 / 40.0;

static const double DP_A41 = 44.0 / 45.0;
static const double DP_A42 = -56.0 / 15.0;
static const double DP_A43 = 32.0 / 9.0;

static const double DP_A51 = 19372.0 / 6561.0;
static const double DP_A52 = -25360.0 / 2187.0;
static const double DP_A53 = 64448.0 / 6561.0;
static const double DP_A54 = -212.0 / 729.0;

static const double DP_A61 = 9017.0 / 3168.0;
static const double DP_A62 = -355.0 / 33.0;
static const double DP_A63 = 46732.0 / 5247.0;
static const double DP_A64 = 49.0 / 176.0;
static const double DP_A65 = -5103.0 / 18656.0;

/* Веса 5-го порядка точности (совпадают со строкой a_7j — свойство FSAL) */
static const double DP_B1  = 35.0 / 384.0;
static const double DP_B3  = 500.0 / 1113.0;
static const double DP_B4  = 125.0 / 192.0;
static const double DP_B5  = -2187.0 / 6784.0;
static const double DP_B6  = 11.0 / 84.0;

/* Коэффициенты разности e_j = b_j - b_hat_j для оценки локальной ошибки */
static const double DP_E1  = 71.0 / 57600.0;
static const double DP_E3  = -71.0 / 16695.0;
static const double DP_E4  = 71.0 / 1920.0;
static const double DP_E5  = -17253.0 / 339200.0;
static const double DP_E6  = 22.0 / 525.0;
static const double DP_E7  = -1.0 / 40.0;

static inline double clamp_d(double v, double lo, double hi) {
    return (v < lo) ? lo : ((v > hi) ? hi : v);
}

void compass_pk_init_default_params(CompassPopPKParams *params) {
    if (!params) return;
    memset(params, 0, sizeof(CompassPopPKParams));

    params->dose_mg                    = 20.0;
    params->dosing_interval_hours      = 12.0;
    params->total_doses_count          = 14;
    params->bioavailability_f          = 0.78;
    params->ka_per_h                   = 0.85;
    params->v_central_l                = 140.0;
    params->v_periph_l                 = 180.0;
    params->q_intercomp_l_h            = 18.0;

    params->vmax_cyp2d6_mg_h           = 3.20;
    params->km_cyp2d6_ng_ml            = 65.0;
    params->vmax_cyp3a4_mg_h           = 1.45;
    params->km_cyp3a4_ng_ml            = 110.0;

    params->cyp2d6_activity_score      = 1.00;
    params->cyp2d6_inhibitor_ratio_sum = 0.0;
    params->cyp2d6_mbi_remaining_frac  = 1.00;
    params->cyp3a4_inhibitor_ratio_sum = 0.0;
    params->cyp3a4_mbi_remaining_frac  = 1.00;
    params->hepatic_child_pugh_factor  = 1.00;
    params->egfr_ml_min                = 90.0;
    params->cl_renal_baseline_l_h      = 1.80;

    params->c_therapeutic_min_ng_ml    = 65.0;
    params->c_therapeutic_max_ng_ml    = 240.0;
    params->c_toxic_threshold_ng_ml    = 320.0;
}

/*
 * Вычисление правой части 5-мерной системы ОДУ dy/dt = f(y):
 * y[0] = A_gut (мг)
 * y[1] = A_central (мг)
 * y[2] = A_periph (мг)
 * y[3] = A_eliminated (мг)
 * y[4] = AUC (нг * ч / мл)
 */
static void pk_ode_rhs(
    const double y[PK_STATE_DIM],
    const CompassPopPKParams *p,
    double k12,
    double k21,
    double km_2d6_app,
    double km_3a4_app,
    double vmax_2d6_eff,
    double vmax_3a4_eff,
    double cl_renal_eff,
    double dydt[PK_STATE_DIM]
) {
    double a_gut = fmax(0.0, y[0]);
    double a_cen = fmax(0.0, y[1]);
    double a_per = fmax(0.0, y[2]);

    /* Концентрация в центральной плазме: мг / л * 1000 = нг / мл */
    double c_plasma_ng_ml = (a_cen / p->v_central_l) * 1000.0;

    /* Мгновенная скорость метаболической элиминации по Михаэлису-Ментен (мг/ч):
     * Rate_MM = V_max_eff * C_c / (K_m_app + C_c)
     */
    double rate_2d6_mg_h = (vmax_2d6_eff * c_plasma_ng_ml) / (km_2d6_app + c_plasma_ng_ml + PK_EPS);
    double rate_3a4_mg_h = (vmax_3a4_eff * c_plasma_ng_ml) / (km_3a4_app + c_plasma_ng_ml + PK_EPS);

    /* Почечная элиминация первого порядка (мг/ч): CL_renal (л/ч) * (A_c / V_c) (мг/л) */
    double rate_renal_mg_h = cl_renal_eff * (a_cen / p->v_central_l);

    double total_elim_mg_h = rate_2d6_mg_h + rate_3a4_mg_h + rate_renal_mg_h;
    double absorption_mg_h = p->ka_per_h * a_gut;
    double dist_flux_mg_h  = k12 * a_cen - k21 * a_per;

    dydt[0] = -absorption_mg_h;
    dydt[1] = absorption_mg_h - dist_flux_mg_h - total_elim_mg_h;
    dydt[2] = dist_flux_mg_h;
    dydt[3] = total_elim_mg_h;
    dydt[4] = c_plasma_ng_ml;
}

int32_t compass_solve_poppk_rk45(
    const CompassPopPKParams *params,
    CompassPopPKSolution *out_sol
) {
    if (!params || !out_sol) {
        return -1;
    }
    if (params->v_central_l <= 0.1 || params->v_periph_l <= 0.1 || params->dosing_interval_hours <= 0.25) {
        return -2;
    }
    memset(out_sol, 0, sizeof(CompassPopPKSolution));

    /* Предвычисление эффективных фармакокинетических констант */
    double k12 = fmax(0.0, params->q_intercomp_l_h) / params->v_central_l;
    double k21 = fmax(0.0, params->q_intercomp_l_h) / params->v_periph_l;

    double inh_2d6 = 1.0 + fmax(0.0, params->cyp2d6_inhibitor_ratio_sum);
    double inh_3a4 = 1.0 + fmax(0.0, params->cyp3a4_inhibitor_ratio_sum);

    double km_2d6_app = fmax(1.0, params->km_cyp2d6_ng_ml) * inh_2d6;
    double km_3a4_app = fmax(1.0, params->km_cyp3a4_ng_ml) * inh_3a4;

    double hep_scale  = clamp_d(params->hepatic_child_pugh_factor, 0.05, 1.5);
    double as_2d6     = clamp_d(params->cyp2d6_activity_score, 0.02, 3.5);
    double mbi_2d6    = clamp_d(params->cyp2d6_mbi_remaining_frac, 0.02, 1.0);
    double mbi_3a4    = clamp_d(params->cyp3a4_mbi_remaining_frac, 0.02, 1.0);

    double vmax_2d6_eff = fmax(0.0, params->vmax_cyp2d6_mg_h) * as_2d6 * hep_scale * mbi_2d6;
    double vmax_3a4_eff = fmax(0.0, params->vmax_cyp3a4_mg_h) * hep_scale * mbi_3a4;
    double cl_renal_eff = fmax(0.0, params->cl_renal_baseline_l_h) * clamp_d(params->egfr_ml_min / 90.0, 0.05, 2.0);

    double bio_dose_mg = clamp_d(params->bioavailability_f, 0.01, 1.0) * fmax(0.0, params->dose_mg);

    /* Инициализация вектора состояния в момент t = 0 (первый прием препарата) */
    double y[PK_STATE_DIM] = {0.0, 0.0, 0.0, 0.0, 0.0};
    double total_injected_mass_mg = 0.0;
    int32_t doses_given = 0;
    double next_dose_time_h = 0.0;

    if (params->total_doses_count > 0) {
        y[0] += bio_dose_mg;
        total_injected_mass_mg += bio_dose_mg;
        doses_given = 1;
        next_dose_time_h = params->dosing_interval_hours;
    }

    out_sol->conc_plasma_ng_ml[0] = 0.0;
    out_sol->amount_gut_mg[0]     = y[0];
    out_sol->amount_periph_mg[0]  = 0.0;

    double c_max = 0.0;
    double t_max = 0.0;
    double c_max_first_dose = 0.0;

    /* Настройки адаптивного шага Дорманда-Принса RK45 */
    const double rtol = 1e-7;
    const double atol = 1e-9;
    const double h_min = 1e-5;
    const double h_max = 0.50;
    double h = 0.10;

    int32_t accepted_steps = 0;
    int32_t rejected_steps = 0;

    double k1[PK_STATE_DIM], k2[PK_STATE_DIM], k3[PK_STATE_DIM], k4[PK_STATE_DIM];
    double k5[PK_STATE_DIM], k6[PK_STATE_DIM], k7[PK_STATE_DIM];
    double y_stage[PK_STATE_DIM];
    double y_next[PK_STATE_DIM];

    int32_t fsal_valid = 0;

    /* Почасовой цикл по точным узлам сетки 0 -> 1 -> 2 ... -> 168 ч */
    for (int hour_idx = 1; hour_idx <= PK_HORIZON_HOURS; ++hour_idx) {
        double t_cur = (double)(hour_idx - 1);
        double t_target = (double)hour_idx;

        while (t_cur < t_target - 1e-11) {
            /* Проверяем, не лежит ли внутри [t_cur, t_target] очередное событие приема дозы */
            double segment_end = t_target;
            int32_t hit_dose_boundary = 0;
            if (doses_given < params->total_doses_count &&
                next_dose_time_h > t_cur + 1e-11 &&
                next_dose_time_h <= t_target + 1e-11) {
                segment_end = next_dose_time_h;
                hit_dose_boundary = 1;
            }

            if (!fsal_valid) {
                pk_ode_rhs(y, params, k12, k21, km_2d6_app, km_3a4_app,
                           vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k1);
                fsal_valid = 1;
            }

            double h_step = h;
            if (t_cur + h_step >= segment_end) {
                h_step = segment_end - t_cur;
            }

            /* Стадия 2 */
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                y_stage[i] = y[i] + h_step * (DP_A21 * k1[i]);
            }
            pk_ode_rhs(y_stage, params, k12, k21, km_2d6_app, km_3a4_app,
                       vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k2);

            /* Стадия 3 */
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                y_stage[i] = y[i] + h_step * (DP_A31 * k1[i] + DP_A32 * k2[i]);
            }
            pk_ode_rhs(y_stage, params, k12, k21, km_2d6_app, km_3a4_app,
                       vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k3);

            /* Стадия 4 */
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                y_stage[i] = y[i] + h_step * (DP_A41 * k1[i] + DP_A42 * k2[i] + DP_A43 * k3[i]);
            }
            pk_ode_rhs(y_stage, params, k12, k21, km_2d6_app, km_3a4_app,
                       vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k4);

            /* Стадия 5 */
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                y_stage[i] = y[i] + h_step * (DP_A51 * k1[i] + DP_A52 * k2[i] + DP_A53 * k3[i] + DP_A54 * k4[i]);
            }
            pk_ode_rhs(y_stage, params, k12, k21, km_2d6_app, km_3a4_app,
                       vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k5);

            /* Стадия 6 */
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                y_stage[i] = y[i] + h_step * (DP_A61 * k1[i] + DP_A62 * k2[i] + DP_A63 * k3[i] +
                                              DP_A64 * k4[i] + DP_A65 * k5[i]);
            }
            pk_ode_rhs(y_stage, params, k12, k21, km_2d6_app, km_3a4_app,
                       vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k6);

            /* Решение 5-го порядка и стадия 7 (FSAL) */
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                y_next[i] = y[i] + h_step * (DP_B1 * k1[i] + DP_B3 * k3[i] + DP_B4 * k4[i] +
                                             DP_B5 * k5[i] + DP_B6 * k6[i]);
            }
            pk_ode_rhs(y_next, params, k12, k21, km_2d6_app, km_3a4_app,
                       vmax_2d6_eff, vmax_3a4_eff, cl_renal_eff, k7);

            /* Оценка масштабированной нормы ошибки err между 5-м и 4-м порядком */
            double err_sq_sum = 0.0;
            for (int i = 0; i < PK_STATE_DIM; ++i) {
                double err_i = h_step * (DP_E1 * k1[i] + DP_E3 * k3[i] + DP_E4 * k4[i] +
                                         DP_E5 * k5[i] + DP_E6 * k6[i] + DP_E7 * k7[i]);
                double scale_i = atol + rtol * fmax(fabs(y[i]), fabs(y_next[i]));
                double ratio = err_i / scale_i;
                err_sq_sum += ratio * ratio;
            }
            double err_norm = sqrt(err_sq_sum / (double)PK_STATE_DIM);

            if (err_norm <= 1.0 || h_step <= h_min) {
                /* Шаг принят */
                t_cur += h_step;
                for (int i = 0; i < PK_STATE_DIM; ++i) {
                    y[i] = fmax(0.0, y_next[i]);
                    k1[i] = k7[i]; /* FSAL: переиспользуем k7 как k1 следующего шага */
                }
                accepted_steps++;

                double c_now = (y[1] / params->v_central_l) * 1000.0;
                if (c_now > c_max) {
                    c_max = c_now;
                    t_max = t_cur;
                }
                if (t_cur <= params->dosing_interval_hours && c_now > c_max_first_dose) {
                    c_max_first_dose = c_now;
                }

                /* Если дошли до момента очередного приема дозы, вводим болюс в депо ЖКТ */
                if (hit_dose_boundary && fabs(t_cur - next_dose_time_h) <= 1e-9) {
                    y[0] += bio_dose_mg;
                    total_injected_mass_mg += bio_dose_mg;
                    doses_given++;
                    next_dose_time_h = (double)doses_given * params->dosing_interval_hours;
                    fsal_valid = 0; /* После импульса дозы сбрасываем FSAL */
                }

                double factor = (err_norm > 1e-15) ? (0.9 * pow(err_norm, -0.2)) : 2.5;
                factor = clamp_d(factor, 0.2, 4.0);
                h = clamp_d(h_step * factor, h_min, h_max);
            } else {
                /* Шаг отклонен: уменьшаем шаг без смены состояния */
                rejected_steps++;
                double factor = clamp_d(0.9 * pow(err_norm, -0.25), 0.15, 0.8);
                h = fmax(h_min, h_step * factor);
            }
        }

        /* Запись точного значения в узле часа hour_idx */
        double c_hour = (y[1] / params->v_central_l) * 1000.0;
        out_sol->conc_plasma_ng_ml[hour_idx] = c_hour;
        out_sol->amount_gut_mg[hour_idx]     = y[0];
        out_sol->amount_periph_mg[hour_idx]  = y[2];
    }

    /* Проверка закона сохранения массы: |A_gut + A_cen + A_per + A_elim - Total_Injected| */
    double final_mass_sum = y[0] + y[1] + y[2] + y[3];
    out_sol->mass_balance_error_mg = fabs(final_mass_sum - total_injected_mass_mg);

    out_sol->c_max_ng_ml         = c_max;
    out_sol->t_max_hours         = t_max;
    out_sol->auc_0_168_ng_h_ml   = y[4];
    out_sol->rk45_accepted_steps = accepted_steps;
    out_sol->rk45_rejected_steps = rejected_steps;

    /* Оценка равновесной остаточной концентрации C_trough,ss перед последним приемом */
    int last_dose_hour = (int)((params->total_doses_count - 1) * params->dosing_interval_hours);
    if (last_dose_hour >= 1 && last_dose_hour <= PK_HORIZON_HOURS) {
        out_sol->c_trough_ss_ng_ml = out_sol->conc_plasma_ng_ml[last_dose_hour];
    } else {
        out_sol->c_trough_ss_ng_ml = out_sol->conc_plasma_ng_ml[PK_HORIZON_HOURS];
    }

    out_sol->accumulation_index_rac = (c_max_first_dose > 1e-3)
        ? (c_max / c_max_first_dose)
        : 1.0;

    /* Оценка эффективного периода полувыведения T_{1/2} в стационарном состоянии */
    double c_mid = 0.5 * (c_max + out_sol->c_trough_ss_ng_ml);
    double cl_eff_l_h = ((vmax_2d6_eff / (km_2d6_app + c_mid)) * 1000.0) +
                        ((vmax_3a4_eff / (km_3a4_app + c_mid)) * 1000.0) +
                        cl_renal_eff;
    double v_dss_l = params->v_central_l + params->v_periph_l;
    out_sol->effective_half_life_h = (cl_eff_l_h > 1e-4)
        ? (0.69314718056 * v_dss_l / cl_eff_l_h)
        : 120.0;

    /* Классификация фармакокинетического риска и расчет коэффициента коррекции дозы */
    double target_cmax = 0.5 * (params->c_therapeutic_min_ng_ml + params->c_therapeutic_max_ng_ml);
    out_sol->recommended_dose_adjustment_factor = (c_max > 1.0)
        ? clamp_d(target_cmax / c_max, 0.20, 2.00)
        : 1.0;

    if (c_max >= params->c_toxic_threshold_ng_ml) {
        out_sol->risk_classification_code = PK_RISK_TOXIC_HARD_STOP;
    } else if (c_max > params->c_therapeutic_max_ng_ml) {
        out_sol->risk_classification_code = PK_RISK_SUPRATHERAPEUTIC_WARN;
    } else if (c_max < params->c_therapeutic_min_ng_ml) {
        out_sol->risk_classification_code = PK_RISK_SUBTHERAPEUTIC;
    } else {
        out_sol->risk_classification_code = PK_RISK_OPTIMAL_THERAPEUTIC;
    }

    return 0;
}