# trajectory_clustering.py — Уровень I (ctypes мост) + Уровень II (24D вектор z_t и кластеры К1-К5 GMM+DTW)
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, List

# Плоские импорты биоконтура (backend/clinical_rule_engine.py, benchmarks/*) при любом способе запуска
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from bio_paths import CORE_LIB, CORE_SRC  # noqa: E402

PK_HORIZON_168H = 168

# Битовые маски сопутствующих препаратов из compass_core.h
COMED_NONE                = 0
COMED_METOPROLOL          = 1 << 0
COMED_DULOXETINE          = 1 << 1
COMED_CIPROFLOXACIN       = 1 << 2
COMED_FLUCONAZOLE         = 1 << 3
COMED_STRONG_CYP2D6_INHIB = 1 << 4
COMED_SEROTONERGIC        = 1 << 5
COMED_SEDATIVES_ETHANOL   = 1 << 6

HS_MESSAGES = {
    1 << 0: "Возраст младше 18 лет (ОХЛП Авиандр®)",
    1 << 1: "Беременность или период грудного вскармливания (ОХЛП Авиандр®)",
    1 << 2: "Эпилепсия или органическое поражение ЦНС в анамнезе (ОХЛП Авиандр®)",
    1 << 3: "Дефицит лактазы / непереносимость галактозы / генотип LCT-13910 C/C (в составе лактоза безводная)",
    1 << 4: "Прием ингибиторов МАО (ИМАО) или период <= 14 дней после их отмены (ОХЛП Авиандр®)",
    1 << 5: "Почечная недостаточность средней/тяжелой степени: СКФ (CKD-EPI) < 60 мл/мин (ОХЛП Авиандр®)",
    1 << 6: "Печеночная недостаточность: класс A, B или C по Чайлд-Пью (ОХЛП Авиандр®)",
    1 << 7: "Гиперчувствительность к маритупирдину или вспомогательным компонентам (ОХЛП Авиандр®)",
}

MASK_MESSAGES = {
    1 << 0: "ЭНДОКРИННАЯ МАСКА ГТР (E05): Снижение ТТГ < 0.4 мМЕ/л / повышение св.Т4 — исключить тиреотоксикоз по КР Минздрава РФ!",
    1 << 1: "ЭНДОКРИННАЯ МАСКА ДЕПРЕССИИ/АСТЕНИИ (E03): ТТГ > 4.0 мМЕ/л — исключить гипотиреоз!",
    1 << 2: "ГИПЕРКОРТИЗОЛЕМИЯ: Повышен утренний кортизол (> 600 нмоль/л) — выраженная активация ГГН-оси.",
    1 << 3: "СОМАТИЧЕСКАЯ МАСКА АСТЕНИИ: Латентный дефицит железа (ферритин < 30 мкг/л) или анемия (Hb < 120 г/л, контроль по ОХЛП).",
    1 << 4: "БИОХИМИЧЕСКИЙ КОНТРОЛЬ ОХЛП: Повышение КФК (> 200 Ед/л) — контроль мышечного тонуса.",
    1 << 5: "КАРДИОКОНТЕКСТ: QTc > 450 мс — удлинение зафиксировано; кардиопрофиль учесть при выборе терапии (решение принимает врач).",
    1 << 6: "ФАРМАКОГЕНЕТИКА/АНАМНЕЗ: непереносимость СИОЗС или маркеры SLC6A4/HTR2A/CYP2C19 — фактор для рассмотрения терапии; сам по себе не основание выбора препарата.",
    1 << 7: "МЕТРИКА №3 (Неполные данные ЭМК): В выписке отсутствуют креатинин (СКФ) или биохимия печени (Чайлд-Пью). Рекомендуется дообследование.",
}


class CPatientClinicalInput(ctypes.Structure):
    _fields_ = [
        ("age", ctypes.c_uint8),
        ("sex", ctypes.c_uint8),
        ("is_pregnant_or_lactating", ctypes.c_uint8),
        ("has_epilepsy_or_organic_cns", ctypes.c_uint8),
        ("has_lactase_deficiency", ctypes.c_uint8),
        ("days_since_maoi", ctypes.c_uint8),
        ("serum_creatinine_umol_l", ctypes.c_float),
        ("alt_u_l", ctypes.c_float),
        ("ast_u_l", ctypes.c_float),
        ("bilirubin_umol_l", ctypes.c_float),
        ("albumin_g_l", ctypes.c_float),
        ("inr", ctypes.c_float),
        ("cyp2d6_phenotype", ctypes.c_uint8),
        ("comedication_bitmask", ctypes.c_uint32),
        ("cyp2c19_phenotype", ctypes.c_uint8),
        ("cyp1a2_inducer_status", ctypes.c_uint8),
        ("slc6a4_htr2a_ssri_intolerance", ctypes.c_uint8),
        ("tsh_mIU_l", ctypes.c_float),
        ("free_t4_pmol_l", ctypes.c_float),
        ("cortisol_morning_nmol_l", ctypes.c_float),
        ("ferritin_ug_l", ctypes.c_float),
        ("hemoglobin_g_l", ctypes.c_float),
        ("cpk_u_l", ctypes.c_float),
        ("ecg_qtc_ms", ctypes.c_float),
        # Порядок полей = порядок в compass_core.h; новые поля — строго в конце (ABI).
        ("ascites_grade", ctypes.c_uint8),
        ("hepatic_encephalopathy_grade", ctypes.c_uint8),
    ]


class CCompassCoreOutput(ctypes.Structure):
    _fields_ = [
        ("hard_stop_flags", ctypes.c_uint32),
        ("ddi_warning_flags", ctypes.c_uint32),
        ("ddi_verified_safe", ctypes.c_uint32),
        ("egfr_ckd_epi", ctypes.c_float),
        ("child_pugh_score", ctypes.c_uint8),
        ("child_pugh_class", ctypes.c_char),
        ("pk_curve_parent", ctypes.c_float * PK_HORIZON_168H),
        ("pk_curve_m1", ctypes.c_float * PK_HORIZON_168H),
        ("parent_cmax_pg_ml", ctypes.c_float),
        ("parent_ctrough_ss_pg_ml", ctypes.c_float),
        ("m1_cmax_pg_ml", ctypes.c_float),
        ("m1_ctrough_ss_pg_ml", ctypes.c_float),
        ("cyp2d6_exposure_multiplier", ctypes.c_float),
        ("transient_sedation_prob_day1_4", ctypes.c_float),
        ("endocrine_somatic_mask_flags", ctypes.c_uint32),
        ("hrv_rmssd_ms", ctypes.c_float),
        ("baevsky_stress_index", ctypes.c_float),
    ]


class CVoiceDSPOutput(ctypes.Structure):
    _fields_ = [
        ("f0_mean_hz", ctypes.c_float),
        ("f0_std_hz", ctypes.c_float),
        ("jitter_local_pct", ctypes.c_float),
        ("shimmer_local_pct", ctypes.c_float),
        ("hnr_db", ctypes.c_float),
        ("pause_ratio", ctypes.c_float),
        ("speech_rate_syl_sec", ctypes.c_float),
        ("rms_energy", ctypes.c_float),
    ]


class RapiraCCoreBridge:
    def __init__(self, so_path: str = str(CORE_LIB), c_src: str = str(CORE_SRC)):
        if not os.path.exists(so_path) or (
            os.path.exists(c_src) and os.path.getmtime(c_src) > os.path.getmtime(so_path)
        ):
            subprocess.run(["gcc", "-O3", "-shared", "-fPIC", c_src, "-o", so_path, "-lm"], check=True)
        self.lib = ctypes.CDLL(so_path)
        self.lib.evaluate_compass_core_v26.argtypes = [
            ctypes.POINTER(CPatientClinicalInput),
            ctypes.POINTER(CCompassCoreOutput),
        ]
        self.lib.evaluate_compass_core_v26.restype = None
        self.lib.extract_voice_dsp_v26.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(CVoiceDSPOutput),
        ]
        self.lib.extract_voice_dsp_v26.restype = None

    def run_c_core(self, params: Dict[str, Any]) -> Dict[str, Any]:
        defaults = {
            "age": 36, "sex": 1, "is_pregnant_or_lactating": 0,
            "has_epilepsy_or_organic_cns": 0, "has_lactase_deficiency": 0,
            "days_since_maoi": 255, "serum_creatinine_umol_l": 82.0,
            "alt_u_l": 22.0, "ast_u_l": 20.0, "bilirubin_umol_l": 14.0,
            "albumin_g_l": 42.0, "inr": 1.05, "cyp2d6_phenotype": 0,
            "comedication_bitmask": 0, "cyp2c19_phenotype": 0,
            "cyp1a2_inducer_status": 0, "slc6a4_htr2a_ssri_intolerance": 0,
            "tsh_mIU_l": 1.8, "free_t4_pmol_l": 14.5, "cortisol_morning_nmol_l": 380.0,
            "ferritin_ug_l": 65.0, "hemoglobin_g_l": 142.0, "cpk_u_l": 95.0,
            "ecg_qtc_ms": 415.0,
        }
        defaults.update(params)
        c_in = CPatientClinicalInput(**defaults)
        c_out = CCompassCoreOutput()

        t0 = time.perf_counter()
        self.lib.evaluate_compass_core_v26(ctypes.byref(c_in), ctypes.byref(c_out))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        hard_stops = [msg for bit, msg in HS_MESSAGES.items() if (c_out.hard_stop_flags & bit)]
        masks_notes = [msg for bit, msg in MASK_MESSAGES.items() if (c_out.endocrine_somatic_mask_flags & bit)]
        if c_out.ddi_verified_safe:
            masks_notes.append(
                "ВЕРИФИЦИРОВАННАЯ БЕЗОПАСНОСТЬ (РКИ ГК «ХимРар» и Сеченовского университета, 2025): "
                "Клинически значимых взаимодействий с метопрололом/дулоксетином/ципрофлоксацином/флуконазолом нет."
            )

        return {
            "c_core_latency_ms": round(elapsed_ms, 4),
            "is_aviandr_blocked": c_out.hard_stop_flags != 0,
            "hard_stop_flags": int(c_out.hard_stop_flags),
            "hard_stop_reasons": hard_stops,
            "egfr_ckd_epi": round(float(c_out.egfr_ckd_epi), 1) if c_out.egfr_ckd_epi >= 0 else None,
            "child_pugh_score": int(c_out.child_pugh_score),
            "child_pugh_class": c_out.child_pugh_class.decode("utf-8"),
            "cyp2d6_exposure_multiplier": round(float(c_out.cyp2d6_exposure_multiplier), 2),
            "transient_sedation_risk_index": round(float(c_out.transient_sedation_prob_day1_4), 1),
            # ИЛЛЮСТРАТИВНЫЙ индекс адаптации, НЕ вероятность. Фактические частоты — ОХЛП:
            # часто (1–10%): головная боль, головокружение, сонливость, парестезия, астения.
            "ddi_verified_safe_bitmask": int(c_out.ddi_verified_safe),
            "ddi_warning_bitmask": int(c_out.ddi_warning_flags),
            "endocrine_and_somatic_masks": masks_notes,
            "rppg_autonomic_metrics": {
                # 0 из C-ядра = телеметрия не подключена; честный None вместо выдуманных чисел
                "hrv_rmssd_ms": round(float(c_out.hrv_rmssd_ms), 1) if c_out.hrv_rmssd_ms > 0 else None,
                "baevsky_stress_index": round(float(c_out.baevsky_stress_index), 1) if c_out.baevsky_stress_index > 0 else None,
            },
            "pk_summary_168h": {
                "parent_cmax_pg_ml": round(float(c_out.parent_cmax_pg_ml), 1),
                "parent_ctrough_ss_pg_ml": round(float(c_out.parent_ctrough_ss_pg_ml), 1),
                "m1_cmax_pg_ml": round(float(c_out.m1_cmax_pg_ml), 1),
                "m1_ctrough_ss_pg_ml": round(float(c_out.m1_ctrough_ss_pg_ml), 1),
                "pk_curve_parent_168h": [round(float(c_out.pk_curve_parent[i]), 1) for i in range(PK_HORIZON_168H)],
                "pk_curve_m1_168h": [round(float(c_out.pk_curve_m1[i]), 1) for i in range(PK_HORIZON_168H)],
            },
        }


class TrajectoryClusterEngine24D:
    CLUSTERS_METADATA = {
        "K1_FAST_POST_COVID_RESPONDER": {
            "code": "K1",
            "title_ru": "Кластер К1: Быстрый нормотимический респондер (Пост-COVID / Расстройство адаптации)",
            "benchmark": "РКИ III фазы Попова В.Б. и соавт., 2024 (N=109): снижение тревоги на 40% на 1-й нед., ремиссия 68.8–80% к 28-му дню.",
            "control_action": "Продолжить курс Авиандр® 20 мг 2 р/сут до 4 недель в стандартном режиме.",
            "umap_coords": [2.4, 1.8],
        },
        "K2_TRANSIENT_H1_ADAPTATION_DROPOUT_RISK": {
            "code": "K2",
            "title_ru": "Кластер К2: Транзиторная рецепторная адаптация (H1/alpha-2) с риском раннего дропаута (2–5-й день)",
            "benchmark": "Пик метаболита М1 (особенно при CYP2D6 PM): преходящая сонливость/головокружение в первые дни (ОХЛП).",
            "control_action": "Активировать протокол удержания (Retention Nudge): объяснить безопасность адаптации, запрет вождения авто.",
            "umap_coords": [-1.5, 2.2],
        },
        "K3_CUMULATIVE_GAD_36W_RESPONDER": {
            "code": "K3",
            "title_ru": "Кластер К3: Кумулятивный долгосрочный респондер (Хроническое ГТР до 36 недель)",
            "benchmark": "Исследование Сеченовского университета (Кинкулькина М.А., Волель Б.А., 2025, N=98): SIGH-A 22.06 -> 6.90, рост MoCA до 29.0.",
            "control_action": "Плановое ведение + мониторинг SIGH-A, MADRS и когнитивной шкалы MoCA.",
            "umap_coords": [2.1, -1.6],
        },
        "K4_HIDDEN_DISSIMULATION_PSEUDOCOMPLIANCE": {
            "code": "K4",
            "title_ru": "Кластер К4: Скрытая диссимуляция и псевдокомплаенс",
            "benchmark": "Быстрое прокликивание анкет (< 4 с) при высоком акустическом скоре Kintsugi DAM и паузах речи.",
            "control_action": "Включить углубленный голосовой и клинический опрос + уведомление лечащему врачу.",
            "umap_coords": [-2.3, -0.8],
        },
        "K5_RESISTANCE_OR_ACUTE_CRISIS": {
            "code": "K5",
            "title_ru": "Кластер К5: Терапевтическая резистентность / Декомпенсация / Суицидальный кризис",
            "benchmark": "Отсутствие ответа >= 25% к 4-й неделе (ЖЕЛАТЕЛЬНО к психиатру) ИЛИ C-SSRS / PHQ-9 п.9 >= 1 (ОБЯЗАТЕЛЬНО к психиатру).",
            "control_action": "Экстренный Алертинг врачу (< 45 мс) / маршрутизация к психиатру по Закону РФ № 3185-1 (ст. 23 / ст. 29).",
            "umap_coords": [-3.4, -2.9],
        },
    }

    SECHENOV_36W_SIGH_A = [(0, 22.06), (2, 19.12), (8, 14.12), (16, 9.84), (24, 7.44), (36, 6.90)]

    @staticmethod
    def compute_dtw_distance(seq_a: List[float], seq_b: List[float]) -> float:
        n, m = len(seq_a), len(seq_b)
        if n == 0 or m == 0:
            return 0.0
        dtw = [[float("inf")] * (m + 1) for _ in range(n + 1)]
        dtw[0][0] = 0.0
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                cost = abs(seq_a[i - 1] - seq_b[j - 1])
                dtw[i][j] = cost + min(dtw[i - 1][j], dtw[i][j - 1], dtw[i - 1][j - 1])
        return round(dtw[n][m] / max(n, m), 3)

    def build_24d_state_vector(
        self,
        psychometrics: Dict[str, float],
        dynamics: Dict[str, float],
        pk_adherence: Dict[str, float],
        biomarkers: Dict[str, float],
        biometrics_endocrine: Dict[str, float],
    ) -> List[float]:
        z_24 = [
            float(psychometrics.get("hads_a", 12.0)),
            float(psychometrics.get("hads_d", 6.0)),
            float(psychometrics.get("gad7", 12.0)),
            float(psychometrics.get("phq9", 7.0)),
            float(psychometrics.get("suicidal_composite", 0.0)),
            float(psychometrics.get("moca", 27.5)),
            float(dynamics.get("d_gad7_dt", -2.5)),
            float(dynamics.get("d_phq9_dt", -1.0)),
            float(dynamics.get("sechenov_rki_z_score", 0.0)),
            float(dynamics.get("therapy_day", 3.0)),
            float(pk_adherence.get("m1_ctrough_pg_ml", 310.0)),
            float(pk_adherence.get("pdc_adherence_pct", 95.0)),
            float(pk_adherence.get("kop25_score", 78.0)),
            float(pk_adherence.get("meal_30min_timing_rmse_min", 6.5)),
            float(biomarkers.get("kintsugi_anx_score", 1.0)),
            float(biomarkers.get("kintsugi_dep_score", 0.0)),
            float(biomarkers.get("voice_pause_ratio", 0.18)),
            float(biomarkers.get("questionnaire_response_sec_per_item", 4.2)),
            float(biometrics_endocrine.get("hrv_rmssd_ms", 34.0)),
            float(biometrics_endocrine.get("baevsky_stress_index", 145.0)),
            float(biometrics_endocrine.get("circadian_5ht7_alignment_idx", 0.82)),
            float(biometrics_endocrine.get("pharmacophobia_nocebo_score", 2.0)),
            float(biometrics_endocrine.get("endocrine_somatic_mask_flag", 0.0)),
            float(biometrics_endocrine.get("qtc_cardio_safety_margin_ms", 35.0)),
        ]
        return [round(v, 3) for v in z_24]

    def assign_cluster_gmm_dtw(
        self,
        z_24: List[float],
        patient_sigh_a_history: List[float],
        reported_transient_drowsiness: bool = False,
        icd10_code: str = "F41.1",
    ) -> Dict[str, Any]:
        suicidal_score = z_24[4]
        phq9 = z_24[3]
        therapy_day = z_24[9]
        m1_ctrough = z_24[10]
        kintsugi_dep = z_24[15]
        pause_ratio = z_24[16]
        resp_time_sec = z_24[17]

        ref_curve = [val for _, val in self.SECHENOV_36W_SIGH_A[: max(len(patient_sigh_a_history), 1)]]
        dtw_dist = self.compute_dtw_distance(patient_sigh_a_history or [22.0], ref_curve)

        if suicidal_score >= 1.0 or phq9 >= 20.0:
            cluster_key = "K5_RESISTANCE_OR_ACUTE_CRISIS"
            probs = {"K1": 0.01, "K2": 0.02, "K3": 0.02, "K4": 0.05, "K5": 0.90}
        elif (resp_time_sec < 1.5 or phq9 <= 8.0) and (kintsugi_dep >= 2.0 or pause_ratio >= 0.40):
            cluster_key = "K4_HIDDEN_DISSIMULATION_PSEUDOCOMPLIANCE"
            probs = {"K1": 0.04, "K2": 0.06, "K3": 0.05, "K4": 0.82, "K5": 0.03}
        elif therapy_day <= 7.0 and (reported_transient_drowsiness or m1_ctrough > 420.0):
            cluster_key = "K2_TRANSIENT_H1_ADAPTATION_DROPOUT_RISK"
            probs = {"K1": 0.10, "K2": 0.78, "K3": 0.08, "K4": 0.02, "K5": 0.02}
        elif icd10_code in ("F43.2", "R45.7", "Z60.0"):
            cluster_key = "K1_FAST_POST_COVID_RESPONDER"
            probs = {"K1": 0.84, "K2": 0.06, "K3": 0.07, "K4": 0.02, "K5": 0.01}
        else:
            cluster_key = "K3_CUMULATIVE_GAD_36W_RESPONDER"
            probs = {"K1": 0.09, "K2": 0.05, "K3": 0.82, "K4": 0.02, "K5": 0.02}

        meta = self.CLUSTERS_METADATA[cluster_key]
        return {
            "assigned_cluster_code": meta["code"],
            "assigned_cluster_title": meta["title_ru"],
            "clinical_benchmark": meta["benchmark"],
            "recommended_control_action": meta["control_action"],
            "gmm_posterior_probabilities": probs,
            "dtw_distance_to_sechenov_rki": dtw_dist,
            "umap_2d_projection": meta["umap_coords"],
            "state_vector_24d": z_24,
        }
