#ifndef COMPASS_CORE_H
#define COMPASS_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Единый бинарный контракт нативного ядра COMPASS-HYBRID v2.6 (libcompass_core.so)
 * Связка C11 <-> Python через cffi (ABI/API без копирования памяти).
 */

typedef struct {
    uint8_t age;                           /* Возраст (< 18 лет -> безусловная блокировка) */
    uint8_t sex;                           /* Пол: 0 — женский, 1 — мужской (для СКФ CKD-EPI 2021) */
    uint8_t is_pregnant_or_lactating;      /* Беременность или период лактации (0 или 1) */
    uint8_t has_epilepsy_or_organic_cns;   /* Эпилепсия или органическое поражение ЦНС (0 или 1) */
    uint8_t has_lactase_deficiency;        /* Дефицит лактазы / непереносимость галактозы / LCT C/C (0 или 1) */
    uint8_t days_since_maoi;               /* Полных суток с момента отмены ИМАО (<= 14 -> блокировка) */
    float serum_creatinine_umol_l;         /* Креатинин сыворотки крови (мкмоль/л) */
    float alt_u_l;                         /* АЛТ (Ед/л) */
    float ast_u_l;                         /* АСТ (Ед/л) */
    float bilirubin_umol_l;                /* Билирубин общий (мкмоль/л) */
    float albumin_g_l;                     /* Альбумин (г/л) */
    float inr;                             /* МНО (для расчёта Чайлд-Пью) */
    uint8_t cyp2d6_phenotype;              /* 0 = NM, 1 = IM, 2 = PM (медленный), 3 = UM */
    uint8_t cyp2c19_phenotype;             /* Панель фармакогенетики CYP2C19 */
    uint8_t cyp1a2_inducer_status;         /* Статус индукции/ингибирования CYP1A2 */
    uint8_t slc6a4_htr2a_ssri_intolerance; /* Маркеры непереносимости СИОЗС (SLC6A4 / HTR2A) */
    uint32_t comedication_bitmask;         /* Битовая маска сопутствующих препаратов ГРЛС */
    float tsh_mIU_l;                       /* ТТГ (мМЕ/л) — исключение тиреотоксикоза/гипотиреоза */
    float free_t4_pmol_l;                  /* Свободный Т4 (пмоль/л) */
    float cortisol_morning_nmol_l;         /* Утренний кортизол (нмоль/л) */
    float ferritin_ug_l;                   /* Ферритин (мкг/л, порог < 30) */
    float hemoglobin_g_l;                  /* Гемоглобин (г/л, порог < 120) */
    float cpk_u_l;                         /* КФК (Ед/л) */
    float ecg_qtc_ms;                      /* Интервал QTc на ЭКГ (мс) */
} PatientClinicalInput;

typedef struct {
    uint32_t hard_stop_flags;              /* Битовая маска 8 абсолютных противопоказаний ОХЛП Авиандр® */
    uint32_t ddi_warning_flags;            /* Флаги опасных межлекарственных взаимодействий */
    uint32_t ddi_verified_safe;            /* Безопасные комбинации (метопролол, дулоксетин, ципрофлоксацин, флуконазол) */
    float egfr_ckd_epi;                    /* Расчётная СКФ (мл/мин/1.73 м2, порог < 60) */
    uint8_t child_pugh_score;              /* Сумма баллов Чайлд-Пью (5..15) и класс A/B/C */
    float pk_curve_parent[168];            /* Почасовая концентрация маритупирдина (пг/мл) на 168 ч (7 суток) */
    float pk_curve_m1[168];                /* Почасовая концентрация активного метаболита М1 (пг/мл) на 168 ч */
    float transient_sedation_prob_day1_4;  /* Вероятность преходящей сонливости/головокружения в 1-4 дни */
    uint32_t endocrine_somatic_mask_flags; /* Флаги эндокринных и соматических масок тревоги и запаса QTc */
    float hrv_rmssd_ms;                    /* Вариабельность сердечного ритма RMSSD (мс) */
    float baevsky_stress_index;            /* Индекс вегетативного напряжения Баевского */
} CompassCoreOutput;

int compass_evaluate_patient(const PatientClinicalInput* input, CompassCoreOutput* output);
int compass_scrub_pii(const char* raw_text, char* scrubbed_buffer, int buffer_len);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_CORE_H */
