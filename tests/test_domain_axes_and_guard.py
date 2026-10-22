# tests/test_domain_axes_and_guard.py — формальная сверка 24 осей z_t со
# спецификацией (5 доменов: 6/4/4/4/6) и MissingDataGuard (Метрика №3).
# Прогон: python3 -m tests.test_domain_axes_and_guard
import sys

from backend.services.pipeline import SafeCompassPipeline
from backend.domain.routing import ClinicalCompassRuleEngine
from backend.domain.trajectory import TrajectoryClusterEngine24D

failures = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    # ================= 1. 24 оси: маппинг «вход -> индекс» =================
    eng = TrajectoryClusterEngine24D()
    marker = [10.0 + i for i in range(24)]  # z[i] = 10+i, если маппинг верен
    z = eng.build_24d_state_vector(
        psychometrics={"hads_a": 10.0, "hads_d": 11.0, "gad7": 12.0, "phq9": 13.0,
                       "suicidal_composite": 14.0, "moca": 15.0},
        dynamics={"d_gad7_dt": 16.0, "d_phq9_dt": 17.0, "sechenov_rki_z_score": 18.0,
                  "therapy_day": 19.0},
        pk_adherence={"m1_ctrough_pg_ml": 20.0, "pdc_adherence_pct": 21.0,
                      "kop25_score": 22.0, "meal_30min_timing_rmse_min": 23.0},
        biomarkers={"experimental_voice_anx_class": 24.0, "experimental_voice_dep_class": 25.0,
                    "voice_pause_ratio": 26.0, "questionnaire_response_sec_per_item": 27.0},
        biometrics_endocrine={"hrv_rmssd_ms": 28.0, "baevsky_stress_index": 29.0,
                              "circadian_5ht7_alignment_idx": 30.0,
                              "pharmacophobia_nocebo_score": 31.0,
                              "endocrine_somatic_mask_flag": 32.0,
                              "qtc_cardio_safety_margin_ms": 33.0},
    )
    expected = {
        # Домен 1: психометрический (6 осей)
        0: ("HADS-A", 10.0), 1: ("HADS-D", 11.0), 2: ("GAD-7", 12.0), 3: ("PHQ-9", 13.0),
        4: ("Суицидальный индекс (п.9 + C-SSRS)", 14.0), 5: ("MoCA", 15.0),
        # Домен 2: динамический (4)
        6: ("dGAD-7/dt", 16.0), 7: ("dPHQ-9/dt", 17.0),
        8: ("z-отклонение от коридора РКИ", 18.0), 9: ("Сутки терапии", 19.0),
        # Домен 3: фармакокинетика и комплаенс (4)
        10: ("Ctrough M1 (CYP2D6)", 20.0), 11: ("PDC, %", 21.0),
        12: ("КОП-25", 22.0), 13: ("RMSE «за 30 мин до еды»", 23.0),
        # Домен 4: биомаркерный и поведенческий (4)
        14: ("Акустика: тревога (Kintsugi, experimental)", 24.0),
        15: ("Акустика: депрессия (Kintsugi, experimental)", 25.0),
        16: ("Pause Ratio", 26.0), 17: ("Среднее время ответа на вопрос", 27.0),
        # Домен 5: биометрия, циркадный, эндокринный (6)
        18: ("HRV RMSSD, мс", 28.0), 19: ("Индекс стресса Баевского", 29.0),
        20: ("Circadian Alignment (5-HT7)", 30.0), 21: ("Pharmacophobia/Nocebo", 31.0),
        22: ("Эндокринно-соматическая маска (флаг)", 32.0),
        23: ("QTc-запас кардиобезопасности", 33.0),
    }
    check("Длина вектора = 24", len(z) == 24, f"{len(z)}")
    bad = {i: (name, want, z[i]) for i, (name, want) in expected.items() if abs(z[i] - want) > 0.001}
    check("Все 24 оси соответствуют спецификации доменов", not bad, str(bad) if bad else "6+4+4+4+6")

    # ================= 2. MissingDataGuard (Метрика №3) =================
    pipe = SafeCompassPipeline(ClinicalCompassRuleEngine())

    res = pipe.analyze_text("Осмотр. М, 40 лет. Тревога F41.1. Шкалы: ГТР-7 12б, PHQ9 8б (п9=0), CSSRS=0.")
    masks = " ".join(res["c_core_pk_and_safety"]["endocrine_and_somatic_masks"])
    check("Нет креатинина/печёночных -> предупреждение Метрики №3", "МЕТРИКА №3" in masks)
    check("Нет данных -> статусы DATA_REQUIRED/AMBER (не зелёный)",
          res["overall_status"] in ("AMBER_DATA_REQUIRED", "AMBER_REVIEW"), res["overall_status"])
    confirm = {c["field"] for c in res["doctor_confirmations_needed"]}
    check("Запрошены подтверждения: эпилепсия/лактаза/ИМАО/психоз/печень",
          {"has_epilepsy_or_organic_cns", "has_lactase_deficiency", "maoi_ever",
           "has_psychotic_symptoms", "hepatic_insufficiency_documented"} <= confirm,
          str(sorted(confirm)))

    res2 = pipe.analyze_text(
        "Осмотр. Женщина, 40 лет. F41.1. Креат. 80 мкмоль/л, печеночные пробы в норме, Чайлд-Пью: норма. "
        "Шкалы: ГТР-7 12б, PHQ9 8б (п9=0), CSSRS=0.",
        # Ключи подтверждений — из CONFIRMATIONS (ehr_extract.py): doctor_confirmations_needed
        # отдаёт ПОЛЯ, а API принимает ключи вида no_*; флаг строго True = «врач подтвердил».
        confirmations={"no_epilepsy_or_organic_cns": True,
                       "no_lactase_deficiency": True,
                       "no_maoi_history": True,
                       "no_pregnancy_lactation": True,
                       "no_hepatic_insufficiency": True,
                       "no_suicidal_ideation": True,
                       "no_psychotic_symptoms": True})
    check("После подтверждений врача — green путь доступен (in-label)",
          res2["overall_status"] == "GREEN_IN_LABEL", res2["overall_status"])
    check("Ни одного исключения на пробелах в данных — guard работает без exception", True)

    print()
    if failures:
        print(f"[FAIL] {failures}")
        return 1
    print("[OK] 24 оси соответствуют спецификации; MissingDataGuard верифицирован.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
