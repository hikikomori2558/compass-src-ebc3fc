// compass_core.h — Бинарный контракт C11 ядра команды 20-П «Rapira» (COMPASS-HYBRID v2.6)
#ifndef COMPASS_CORE_H
#define COMPASS_CORE_H

#include <stdint.h>

#define PK_HORIZON_168H 168 // 7 суток (168 часов) почасового RK4 моделирования

// Флаги 8 абсолютных противопоказаний ОХЛП «Авиандр®» (hard_stop_flags)
#define HS_NONE                     0u
#define HS_AGE_UNDER_18             (1u << 0) // 1. Возраст < 18 лет
#define HS_PREGNANCY_LACTATION      (1u << 1) // 2. Беременность / период ГВ
#define HS_EPILEPSY_ORGANIC_CNS     (1u << 2) // 3. Эпилепсия / органическое поражение ЦНС
#define HS_LACTASE_DEFICIENCY_LCT   (1u << 3) // 4. Дефицит лактазы / генотип LCT-13910 C/C (лактоза безводная)
#define HS_MAOI_WASH_OUT_LT_14D     (1u << 4) // 5. Прием ИМАО или <= 14 суток после отмены
#define HS_RENAL_EGFR_LT_60         (1u << 5) // 6. СКФ (CKD-EPI 2021) < 60 мл/мин/1.73м2
#define HS_HEPATIC_CHILD_PUGH_ABC   (1u << 6) // 7. Печеночная недостаточность: класс A, B или C по Чайлд-Пью
#define HS_HYPERSENSITIVITY         (1u << 7) // 8. Гиперчувствительность к маритупирдину

// Битовая маска сопутствующих препаратов (comedication_bitmask)
#define COMED_NONE                  0u
#define COMED_METOPROLOL            (1u << 0) // Субстрат CYP2D6 (безопасно по РКИ ХимРар 2025)
#define COMED_DULOXETINE            (1u << 1) // Ингибитор CYP2D6 (безопасно по РКИ ХимРар 2025)
#define COMED_CIPROFLOXACIN         (1u << 2) // Сильный ингибитор CYP1A2 (безопасно по РКИ ХимРар 2025)
#define COMED_FLUCONAZOLE           (1u << 3) // Сильный ингибитор CYP2C19/P-gp (безопасно по РКИ ХимРар 2025)
#define COMED_STRONG_CYP2D6_INHIB   (1u << 4) // Пароксетин, флуоксетин, хинидин, тербинафин, дифенгидрамин
#define COMED_SEROTONERGIC          (1u << 5) // СИОЗС, ТЦА, трамадол, триптаны, литий, зверобой
#define COMED_SEDATIVES_ETHANOL     (1u << 6) // Бензодиазепины, нейролептики, H1-блокаторы, этанол

// Флаги эндокринно-соматических масок ГТР и кардио-безопасности (endocrine_somatic_mask_flags)
#define MASK_NONE                    0u
#define MASK_THYROTOXICOSIS_E05      (1u << 0) // ТТГ < 0.4 мМЕ/л и/или св.Т4 > 22 пмоль/л (маска тревоги)
#define MASK_HYPOTHYROIDISM_E03      (1u << 1) // ТТГ > 4.0 мМЕ/л (маска депрессии/астении)
#define MASK_HYPERCORTISOLEMIA       (1u << 2) // Утренний кортизол > 600 нмоль/л
#define MASK_IRON_DEFICIENCY_ANEMIA  (1u << 3) // Ферритин < 30 мкг/л или Hb < 120 г/л (контроль Hb по ОХЛП)
#define MASK_ELEVATED_CPK            (1u << 4) // КФК > 200 Ед/л (контроль редкого НЯ по ОХЛП)
#define MASK_QTC_PROLONGED_ADVANTAGE (1u << 5) // QTc > 450 мс: Авиандр безопасен (не удлиняет QT)
#define MASK_SSRI_GENETIC_RESIST     (1u << 6) // Маркеры SLC6A4 S/S, HTR2A или CYP2C19 -> выбор Авиандра
#define MASK_MISSING_CRITICAL_LABS   (1u << 7) // Метрика №3: не указан креатинин или билирубин/альбумин/МНО

typedef struct {
    uint8_t age;
    uint8_t sex;                            // 0 = Ж, 1 = М
    uint8_t is_pregnant_or_lactating;
    uint8_t has_epilepsy_or_organic_cns;
    uint8_t has_lactase_deficiency;         // Дефицит лактазы или генотип LCT-13910 C/C
    uint8_t days_since_maoi;                // 255 = никогда не принимал; <= 14 = жесткая блокировка
    float serum_creatinine_umol_l;          // <= 0 -> не указан в ЭМК (Метрика №3)
    float alt_u_l;
    float ast_u_l;
    float bilirubin_umol_l;                 // Для расчета Чайлд-Пью
    float albumin_g_l;                      // Для расчета Чайлд-Пью
    float inr;                              // МНО для расчета Чайлд-Пью
    uint8_t cyp2d6_phenotype;               // 0=NM, 1=IM, 2=PM (медленный), 3=UM (ультрабыстрый)
    uint32_t comedication_bitmask;
    uint8_t cyp2c19_phenotype;              // 0=NM, 1=PM, 2=UM
    uint8_t cyp1a2_inducer_status;          // 0=Норма, 1=Индуктор
    uint8_t slc6a4_htr2a_ssri_intolerance;  // 1 = Непереносимость СИОЗС в анамнезе / по генетике
    float tsh_mIU_l;                        // ТТГ (0.4 - 4.0)
    float free_t4_pmol_l;                   // Свободный Т4 (9.0 - 22.0)
    float cortisol_morning_nmol_l;          // Кортизол утра
    float ferritin_ug_l;                    // Ферритин (< 30 мкг/л -> дефицит железа)
    float hemoglobin_g_l;                   // Гемоглобин (< 120 г/л)
    float cpk_u_l;                          // КФК
    float ecg_qtc_ms;                       // QTc на ЭКГ (мс)
} PatientClinicalInput;

typedef struct {
    uint32_t hard_stop_flags;
    uint32_t ddi_warning_flags;
    uint32_t ddi_verified_safe;
    float egfr_ckd_epi;
    uint8_t child_pugh_score;               // 0 = норма, 5-6 = A, 7-9 = B, 10-15 = C
    char child_pugh_class;                  // 'N', 'A', 'B', 'C', '?'
    float pk_curve_parent[PK_HORIZON_168H]; // 168 ч концентрации маритупирдина (пг/мл)
    float pk_curve_m1[PK_HORIZON_168H];     // 168 ч концентрации активного метаболита М1 (пг/мл)
    float parent_cmax_pg_ml;
    float parent_ctrough_ss_pg_ml;
    float m1_cmax_pg_ml;
    float m1_ctrough_ss_pg_ml;
    float cyp2d6_exposure_multiplier;
    float transient_sedation_prob_day1_4;   // Прогноз транзиторной сонливости в 1-4 дни (%)
    uint32_t endocrine_somatic_mask_flags;
    float hrv_rmssd_ms;
    float baevsky_stress_index;
} CompassCoreOutput;

typedef struct {
    float f0_mean_hz;
    float f0_std_hz;
    float jitter_local_pct;
    float shimmer_local_pct;
    float hnr_db;
    float pause_ratio;
    float speech_rate_syl_sec;
    float rms_energy;
} VoiceDSPOutput;

void evaluate_compass_core_v26(const PatientClinicalInput* in, CompassCoreOutput* out);
void extract_voice_dsp_v26(const float* pcm_16khz, int n_samples, VoiceDSPOutput* out);

#endif // COMPASS_CORE_H
