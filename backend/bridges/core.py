# backend/bridges/core.py — ctypes-мост к нативному ядру compass_core.c
# (СКФ CKD-EPI, Чайлд-Пью, 8 хард-стопов ОХЛП, RK4 PK 168ч, голосовой DSP).
# Доменная часть (24D-вектор z_t и стратификация К1–К5) — в backend/domain/trajectory.py.
import ctypes
import time
from typing import Any, Dict, List

from backend.bridges.loader import load_core_lib
from backend.paths import CORE_LIB

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
    def __init__(self, so_path: str = str(CORE_LIB), c_src: str = ""):
        # Загрузка и пересборка — только через единый загрузчик backend.bridges.loader.
        # Раньше мост сам звал gcc и мог пересобрать ОДИН compass_core.c поверх общей
        # libcompass_core.so, затирая символы pii_scrubber и лексеров (split-init).
        # Параметры so_path/c_src оставлены для совместимости старых вызовов и не используются.
        self.lib = load_core_lib()
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

    # Значения «поле не передано» для безопасного пути: никаких «нормальных» подстановок.
    # Нули/отрицательные значения ядро трактует как «нет данных» (СКФ < 0, Чайлд-Пью '?').
    UNKNOWN_DEFAULTS = {
        "age": 0, "sex": 0, "is_pregnant_or_lactating": 0, "has_epilepsy_or_organic_cns": 0,
        "has_lactase_deficiency": 0, "days_since_maoi": 255, "serum_creatinine_umol_l": -1.0,
        "alt_u_l": 0.0, "ast_u_l": 0.0, "bilirubin_umol_l": -1.0, "albumin_g_l": -1.0, "inr": -1.0,
        "cyp2d6_phenotype": 0, "comedication_bitmask": 0, "cyp2c19_phenotype": 0,
        "cyp1a2_inducer_status": 0, "slc6a4_htr2a_ssri_intolerance": 0, "tsh_mIU_l": 0.0,
        "free_t4_pmol_l": 0.0, "cortisol_morning_nmol_l": 0.0, "ferritin_ug_l": 0.0,
        "hemoglobin_g_l": 0.0, "cpk_u_l": 0.0, "ecg_qtc_ms": 0.0,
    }

    def run_c_core(self, params: Dict[str, Any], fill_defaults: bool = True) -> Dict[str, Any]:
        """fill_defaults=True — прежнее поведение (демо-сценарии: недостающее = «норма»).
        fill_defaults=False — безопасный путь: недостающее = «нет данных» (см. services/pipeline.py)."""
        if not fill_defaults:
            defaults = dict(self.UNKNOWN_DEFAULTS)
            defaults.update(params)
            return self._run_c_core_impl(defaults)
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
        return self._run_c_core_impl(defaults)

    def _run_c_core_impl(self, defaults: Dict[str, Any]) -> Dict[str, Any]:
        c_in = CPatientClinicalInput(**defaults)
        c_out = CCompassCoreOutput()

        t0 = time.perf_counter()
        self.lib.evaluate_compass_core_v26(ctypes.byref(c_in), ctypes.byref(c_out))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        hard_stops = [msg for bit, msg in HS_MESSAGES.items() if (c_out.hard_stop_flags & bit)]
        masks_notes = [msg for bit, msg in MASK_MESSAGES.items() if (c_out.endocrine_somatic_mask_flags & bit)]
        # Феноконверсия: у генетически нормального/промежуточного метаболизатора
        # сильные ингибиторы CYP2D6 (пароксетин, флуоксетин, хинидин) снижают
        # расчётную активность фермента. Обязательная ремарка из инструкции:
        # валидированного алгоритма коррекции дозы по CYP2D6 нет — только
        # усиленный мониторинг сонливости/головокружения.
        if (c_out.ddi_warning_flags & (1 << 4)) and int(defaults.get("cyp2d6_phenotype") or 0) in (0, 1):
            masks_notes.append(
                "ФЕНОКОНВЕРСИЯ (теоретический и регуляторно обозначенный риск): сильный ингибитор "
                "CYP2D6 снижает расчётную активность фермента — экспозиция маритупирдина и М1 может "
                "вырасти в 1.5–2 раза. Усиленный мониторинг сонливости и головокружения. "
                "Валидированного алгоритма коррекции дозы по CYP2D6 в инструкции нет."
            )
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
