/*
 * c_core/ot_xai_mahalanobis.h
 * ===========================
 * Нативное C11-ядро Варианта 4:
 * 1. Дебиасированная дивергенция Синкхорна-Вассерштейна S_eps(alpha, beta) в лог-области
 *    для нерегулярных 36-недельных клинических траекторий (Cuturi 2013; Feydy et al. 2019).
 * 2. Аналитический сжимающий оцениватель ковариации Ледуа-Вульфа (Ledoit & Wolf 2004) +
 *    факторизация Холецкого + OOD-детектор Махаланобиса в 24D (Lee et al. 2018).
 * 3. Точные двухуровневые коалиционные значения Оуэна (Owen 1977) для 6 физиологических
 *    блоков и поиск минимального контрафактического вмешательства (Mothilal et al. 2020).
 *
 * Проект: COMPASS-HYBRID v2.6 (Команда Rapira)
 */

#ifndef COMPASS_OT_XAI_MAHALANOBIS_H
#define COMPASS_OT_XAI_MAHALANOBIS_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OT_MAX_TIMESTEPS   64
#define OT_STATE_DIM       4
#define OOD_FEATURE_DIM    24
#define OWEN_NUM_UNIONS    6

/* Результат расчета продольного оптимального транспорта Синкхорна-Вассерштейна */
typedef struct {
    double ot_epsilon_ab;              /* Энтропийный транспорт OT_eps(alpha, beta) */
    double ot_self_aa;                 /* Собственный потенциал OT_eps(alpha, alpha) */
    double ot_self_bb;                 /* Собственный потенциал OT_eps(beta, beta) */
    double sinkhorn_divergence;        /* Дебиасированная дивергенция S_eps(alpha, beta) >= 0 */
    double wasserstein_barycentric_shift; /* Смещение скорости ответа относительно когорты */
    int32_t sinkhorn_iterations;       /* Число итераций до сходимости */
    double transport_plan[OT_MAX_TIMESTEPS * OT_MAX_TIMESTEPS]; /* Оптимальная матрица переноса P */
} CompassSinkhornTrajectoryResult;

/* Обученная модель кластера с аналитической регуляризацией Ледуа-Вульфа в 24D */
typedef struct {
    int32_t cluster_id;
    int32_t n_samples;
    double shrinkage_rho;              /* Оптимальный коэффициент сжатия Ледуа-Вульфа rho* in (0, 1] */
    double trace_mu;                   /* Изотропная цель mu = Tr(S) / 24 */
    double condition_number_raw;       /* Оценка обусловленности сырой матрицы S */
    double condition_number_shrunk;    /* Оценка обусловленности регуляризованной матрицы Sigma_LW */
    double log_det_sigma;              /* ln(det(Sigma_LW)) из диагонали Холецкого */
    double centroid[OOD_FEATURE_DIM];  /* 24D центроид кластера */
    double precision_matrix[OOD_FEATURE_DIM * OOD_FEATURE_DIM]; /* Обратная матрица Sigma_LW^{-1} */
    double cholesky_l[OOD_FEATURE_DIM * OOD_FEATURE_DIM];       /* Нижнетреугольный фактор L */
} CompassLedoitWolfClusterModel;

/* Результат OOD-верификации пациента по метрике Махаланобиса */
typedef struct {
    int32_t best_cluster_id;
    double mahalanobis_sq;             /* Квадрат расстояния Махаланобиса D_M^2 */
    double mahalanobis_dist;           /* Евклидова норма в отбеленном пространстве D_M */
    double chi2_p_value;               /* Хвостовая вероятность хи-квадрат (df=24) по Уилсону-Хилферти */
    double chi2_threshold_99;          /* Критический порог chi^2_{24, 0.99} = 42.9798 */
    int32_t is_ood_anomaly;            /* 1, если пациент вне распределения кластеров (OOD) */
    double top_contributing_axes[OOD_FEATURE_DIM]; /* Вклад каждого из 24 признаков в D_M^2 */
} CompassMahalanobisOODResult;

/* Выходная структура точных коалиционных значений Оуэна и контрафактуалов */
typedef struct {
    double baseline_risk_v0;           /* Базовый риск на здоровом референсе v(emptyset) */
    double patient_risk_vn;            /* Полный клинический риск пациента v(N) */
    double owen_phi[OOD_FEATURE_DIM];  /* Точные значения Оуэна для всех 24 признаков */
    double union_attribution[OWEN_NUM_UNIONS]; /* Суммарный вклад каждого из 6 физиологических блоков */
    double efficiency_residual;        /* Погрешность тождества sum(Phi_i) - (v(N) - v(0)) (< 1e-12) */
    int32_t exact_coalition_evals;     /* Число точных вычислений коалиций (4096) */

    /* Контрафактическое минимальное вмешательство */
    double counterfactual_vector[OOD_FEATURE_DIM]; /* Рекомендуемый вектор после коррекции */
    double counterfactual_deltas[OOD_FEATURE_DIM]; /* Сдвиги только по модифицируемым признакам */
    double counterfactual_risk_after;  /* Пост-интервенционный клинический риск */
    double relative_risk_reduction_pct;/* Относительное снижение риска (%) */
    int32_t modified_features_count;   /* Число измененных управляемых факторов */
} CompassOwenXAIResult;

/*
 * Вычисляет дебиасированную дивергенцию Синкхорна-Вассерштейна S_eps(alpha, beta)
 * в логарифмической области между нерегулярной траекторией пациента и эталонной траекторией.
 */
int32_t compass_sinkhorn_trajectory_divergence(
    const double *patient_times_w,
    const double *patient_states,      /* shape: (n_pat, state_dim) */
    const double *patient_weights,     /* shape: (n_pat,), если NULL -> равномерные */
    int32_t n_pat,
    const double *ref_times_w,
    const double *ref_states,          /* shape: (n_ref, state_dim) */
    const double *ref_weights,         /* shape: (n_ref,), если NULL -> равномерные */
    int32_t n_ref,
    int32_t state_dim,
    double epsilon_reg,
    double lambda_time_weight,
    CompassSinkhornTrajectoryResult *out_res
);

/*
 * Обучает аналитический сжимающий оцениватель ковариации Ледуа-Вульфа (Ledoit & Wolf 2004)
 * и строит точную матрицу точности Sigma_LW^{-1} через разложение Холецкого.
 */
int32_t compass_fit_ledoit_wolf_cluster_24d(
    const double *samples_24d,         /* shape: (n_samples, 24) */
    int32_t n_samples,
    int32_t cluster_id,
    CompassLedoitWolfClusterModel *out_model
);

/*
 * Вычисляет квадрат расстояния Махаланобиса D_M^2(x), p-value распределения хи-квадрат (df=24)
 * и детектирует OOD-аномалии.
 */
int32_t compass_evaluate_mahalanobis_ood_24d(
    const CompassLedoitWolfClusterModel *model,
    const double *patient_vec_24d,
    CompassMahalanobisOODResult *out_ood
);

/*
 * Вычисляет точные двухуровневые коалиционные значения Оуэна (Owen 1977) по 6 физиологическим
 * союзам (4096 точных коалиционных прогонов за < 0.1 мс) и находит оптимальное
 * контрафактическое вмешательство с учетом замороженных (иммутабельных) признаков.
 */
int32_t compass_compute_exact_owen_and_counterfactual(
    const double *patient_vec_24d,
    const double *healthy_baseline_24d,
    double target_safe_risk_threshold,
    CompassOwenXAIResult *out_xai
);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_OT_XAI_MAHALANOBIS_H */