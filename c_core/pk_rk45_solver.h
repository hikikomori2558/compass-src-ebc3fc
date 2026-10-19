/*
 * c_core/pk_rk45_solver.h
 * =======================
 * Нативный C11 решатель нелинейных ОДУ двухкомпартментной популяционной
 * фармакокинетики (PopPK) методом Дорманда-Принса RK45(5)4 с FSAL,
 * кинетикой Михаэлиса-Ментен и DDI ингибированием CYP2D6 / CYP3A4.
 *
 * Проект: COMPASS-HYBRID v2.6 (Команда Rapira)
 */

#ifndef COMPASS_PK_RK45_SOLVER_H
#define COMPASS_PK_RK45_SOLVER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PK_HORIZON_HOURS 168
#define PK_GRID_POINTS   (PK_HORIZON_HOURS + 1)
#define PK_STATE_DIM     5

/* Коды фенотипического риска передозировки / неэффективности */
#define PK_RISK_OPTIMAL_THERAPEUTIC   0
#define PK_RISK_SUBTHERAPEUTIC        1
#define PK_RISK_SUPRATHERAPEUTIC_WARN 2
#define PK_RISK_TOXIC_HARD_STOP       3

/* Входные индивидуальные параметры пациента и схемы дозирования */
typedef struct {
    double dose_mg;                   /* Разовая доза препарата (мг), напр. 20.0 */
    double dosing_interval_hours;     /* Интервал дозирования tau (ч), напр. 12.0 */
    int32_t total_doses_count;        /* Число приемов за 168 ч, напр. 14 */
    double bioavailability_f;         /* Абсолютная биодоступность F (0..1), напр. 0.78 */
    double ka_per_h;                  /* Константа скорости всасывания k_a (1/ч), напр. 0.85 */
    double v_central_l;               /* Объем центрального компартмента V_c (л), напр. 140.0 */
    double v_periph_l;                /* Объем периферического компартмента V_p (л), напр. 180.0 */
    double q_intercomp_l_h;           /* Межкомпартментный клиренс Q (л/ч), напр. 18.0 */

    /* Параметры Михаэлиса-Ментен для CYP2D6 и CYP3A4 */
    double vmax_cyp2d6_mg_h;          /* V_max CYP2D6 (мг/ч), напр. 3.20 */
    double km_cyp2d6_ng_ml;           /* K_m CYP2D6 (нг/мл), напр. 65.0 */
    double vmax_cyp3a4_mg_h;          /* V_max CYP3A4 (мг/ч), напр. 1.45 */
    double km_cyp3a4_ng_ml;           /* K_m CYP3A4 (нг/мл), напр. 110.0 */

    /* Индивидуальные ковариаты и DDI-ингибиторы */
    double cyp2d6_activity_score;     /* Фенотип CYP2D6: PM=0.12, IM=0.50, NM=1.0, UM=2.2 */
    double cyp2d6_inhibitor_ratio_sum;/* Сумма [I]/K_i конкурентных ингибиторов CYP2D6 */
    double cyp2d6_mbi_remaining_frac; /* Доля активного фермента при MBI (пароксетин: 0.15..1.0) */
    double cyp3a4_inhibitor_ratio_sum;/* Сумма [I]/K_i конкурентных ингибиторов CYP3A4 */
    double cyp3a4_mbi_remaining_frac; /* Доля активного фермента CYP3A4 при MBI (0.15..1.0) */
    double hepatic_child_pugh_factor; /* A=1.0, B=0.62, C=0.30 */
    double egfr_ml_min;               /* СКФ (мл/мин/1.73м2), норма ~ 90.0 */
    double cl_renal_baseline_l_h;     /* Базовый почечный клиренс (л/ч), напр. 1.8 */

    /* Терапевтические границы концентрации (нг/мл) */
    double c_therapeutic_min_ng_ml;   /* Нижняя граница эффективности, напр. 65.0 */
    double c_therapeutic_max_ng_ml;   /* Верхняя граница терапевтического окна, напр. 240.0 */
    double c_toxic_threshold_ng_ml;   /* Порог токсичности (QTc / НЯ), напр. 320.0 */
} CompassPopPKParams;

/* Выходная структура решения ОДУ RK45 на сетке 0..168 часов */
typedef struct {
    double conc_plasma_ng_ml[PK_GRID_POINTS]; /* Почасовая концентрация C_c(t) [нг/мл] */
    double amount_gut_mg[PK_GRID_POINTS];     /* Профиль массы в депо ЖКТ [мг] */
    double amount_periph_mg[PK_GRID_POINTS];  /* Профиль массы в периферии [мг] */
    double c_max_ng_ml;                       /* Пиковая концентрация C_max [нг/мл] */
    double t_max_hours;                       /* Время достижения пика T_max [ч] */
    double c_trough_ss_ng_ml;                 /* Равновесная остаточная концентрация C_min,ss [нг/мл] */
    double auc_0_168_ng_h_ml;                 /* Интегральная экспозиция AUC_{0-168} (RK45 5-го порядка) */
    double effective_half_life_h;             /* Эффективный период полувыведения T_{1/2} [ч] */
    double accumulation_index_rac;            /* Индекс кумуляции R_ac */
    double mass_balance_error_mg;             /* Погрешность закона сохранения массы [мг] */
    int32_t rk45_accepted_steps;              /* Число принятых адаптивных шагов RK45 */
    int32_t rk45_rejected_steps;              /* Число отклоненных шагов RK45 */
    int32_t risk_classification_code;         /* Код риска (0..3) */
    double recommended_dose_adjustment_factor;/* Коэффициент коррекции дозы (напр. 0.5 при PM/DDI) */
} CompassPopPKSolution;

/*
 * Заполняет структуру CompassPopPKParams клинически выверенными дефолтными
 * параметрами для референсного взрослого пациента (NM CYP2D6, без ингибиторов).
 */
void compass_pk_init_default_params(CompassPopPKParams *params);

/*
 * Интегрирует 5-мерную нелинейную систему ОДУ двухкомпартментной фармакокинетики
 * методом Дорманда-Принса RK45(5)4 на горизонте [0, 168] часов.
 */
int32_t compass_solve_poppk_rk45(
    const CompassPopPKParams *params,
    CompassPopPKSolution *out_sol
);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_PK_RK45_SOLVER_H */