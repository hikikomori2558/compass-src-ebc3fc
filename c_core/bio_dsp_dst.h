/*
 * c_core/bio_dsp_dst.h
 * ====================
 * Нативное C11-ядро языково-инвариантного акустического DSP (Глоттальный фильтр
 * Левинсона-Дарбина LPC-4 + Оператор Тигера-Кайзера + MLE Вейбулла для русской речи)
 * и эвиденциального слияния Демпстера-Шефера с W2-адаптацией Kintsugi DAM.
 *
 * Проект: COMPASS-HYBRID v2.6 (Команда Rapira)
 */

#ifndef COMPASS_BIO_DSP_DST_H
#define COMPASS_BIO_DSP_DST_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Битовые маски решетки подмножеств 2^Theta \ {emptyset} */
#define DST_MASK_REMISSION      0x01u  /* 0b001: {Remission} */
#define DST_MASK_RELAPSE        0x02u  /* 0b010: {Relapse} */
#define DST_MASK_ADVERSE        0x04u  /* 0b100: {AdverseEffect} */
#define DST_MASK_DETERIORATION  0x06u  /* 0b110: {Relapse, AdverseEffect} */
#define DST_MASK_THETA          0x07u  /* 0b111: {Remission, Relapse, AdverseEffect} */

/* Коды доминирующего фенотипа для маршрутизации */
#define PHENOTYPE_CONCORDANT_STABLE          0
#define PHENOTYPE_ACTIVE_RELAPSE             1
#define PHENOTYPE_PK_ADVERSE_RISK            2
#define PHENOTYPE_SOMATOFORM_AGGRAVATION     3
#define PHENOTYPE_MASKED_DISSIMULATION       4

/* Выходная структура C11 DSP-анализатора */
typedef struct {
    double f0_mean_hz;
    double f0_std_hz;
    double jitter_ppq5_pct;
    double shimmer_apq5_pct;
    double glottal_tkeo_instability;
    double glottal_tkeo_kurtosis;
    double weibull_pause_shape_k;
    double weibull_pause_scale_lambda_ms;
    double cognitive_pause_fraction;
    double ru_plosive_closure_fraction;
    double lpc_spectral_tilt_db;
    double hnr_db;
    double snr_db;
    double ru_phonetic_shift_mahalanobis;
    double z_score_depression_invariant;
    double z_score_anxiety_invariant;
} CompassAcousticMetrics;

/* Параметры кросс-лингвистической W2 (Frechet-Wasserstein) калибровки EN -> RU */
typedef struct {
    double mu_en_dep;
    double sigma_en_dep;
    double mu_ru_dep;
    double sigma_ru_dep;
    double mu_en_anx;
    double sigma_en_anx;
    double mu_ru_anx;
    double sigma_ru_anx;
    double shafer_discount_gamma;
} CompassCrossLingualCalib;

/* Выходная структура эвиденциального слияния Демпстера-Шефера */
typedef struct {
    double bpa_ehr[8];         /* Индексация напрямую по битовой маске 1..7 */
    double bpa_dam_disc[8];    /* Дисконтированная по Шеферу масса био-контура */
    double bpa_fused[8];       /* Слитая масса Демпстера-Ягера-Мёрфи */
    double belief[3];          /* [0]: Remission, [1]: Relapse, [2]: AdverseEffect */
    double plausibility[3];    /* [0]: Remission, [1]: Relapse, [2]: AdverseEffect */
    double pignistic_prob[3];  /* BetP для синглетонов */
    double dempster_conflict_k;
    double shafer_reliability_beta;
    double dissimulation_index;
    double aggravation_index;
    double epistemic_uncertainty;
    int32_t dominant_singleton_idx;
    int32_t phenotype_flag_code;
    int32_t recommended_tier;  /* 1, 2 или 3 */
} CompassEvidentialResult;

int32_t compass_analyze_pcm_russian_invariant(
    const double *pcm_samples,
    size_t num_samples,
    int32_t sample_rate,
    CompassAcousticMetrics *out_metrics
);

void compass_fit_weibull_russian_pauses(
    const double *pause_durations_ms,
    size_t num_pauses,
    double *out_shape_k,
    double *out_scale_lambda_ms,
    double *out_plosive_ratio
);

int32_t compass_dempster_shafer_bio_fuse(
    const CompassAcousticMetrics *metrics,
    const CompassCrossLingualCalib *calib,
    double raw_kintsugi_dep_score,
    double raw_kintsugi_anx_score,
    double hama_total,
    double madrs_total,
    double self_report_wellbeing,
    int32_t adverse_event_count,
    double cyp_inhibition_factor,
    double response_latency_sec,
    CompassEvidentialResult *out_result
);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_BIO_DSP_DST_H */