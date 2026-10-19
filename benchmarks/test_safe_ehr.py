#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_safe_ehr.py — регрессионный набор безопасного разбора ЭМК и tri-state движка.

Прогон: python3 benchmarks/test_safe_ehr.py   (код возврата 1, если что-то упало)

Что проверяется (каждый блок = класс ошибок, которые были найдены в старом парсере):
  A. Шесть боевых кейсов из ревью репозитория (отрицание, давность ИМАО, пустая история,
     пропущенный п.9, «непереносимость лактозы отрицает», мг/дл).
  B. Таблица фраз: отрицания, неопределённость, семейный анамнез, сроки ИМАО, единицы,
     даты анализов, возраст/пол, шкалы, комедикация.
  C. Сквозной пайплайн: «неизвестное не бывает зелёным», «противопоказание бьёт всё».
  D. Свойства (fuzz с фиксированным seed) для RulesEngine: инвариант безопасности.
  E. C-ядро: СКФ CKD-EPI 2021 против независимой реализации на Python.
  F. Маршрутизация: None-безопасность и обратная совместимость.
  G. Чек-ин пациента: пропущенный вопрос не превращается в 0.
  H. Синтетический корпус: ни один gt-заблокированный случай не получает не-красный статус.
  I. HTTP-эндпоинт /ehr/analyze-and-route (если окружение позволяет).

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ: тесты и парсер написаны одним автором. Это регрессионный набор, а не
независимая валидация. Независимая проверка — это hold-out из 20+ историй другого автора
(benchmarks/holdout/README.md), прогнанный ДО того, как кто-либо подстраивает парсер под него.
"""
import json
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from clinical_rule_engine import ClinicalCompassRuleEngine  # noqa: E402
from ehr_extract import extract  # noqa: E402
from rules_engine import RulesEngine  # noqa: E402
from safe_pipeline import SafeCompassPipeline  # noqa: E402

AS_OF = date(2026, 10, 18)
FAILURES = []
SKIPPED = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILURES.append(name)


engine = ClinicalCompassRuleEngine()
pipe = SafeCompassPipeline(engine)


def run(text, conf=None):
    return pipe.analyze_text(text, confirmations=conf, as_of=AS_OF)


def ex(text):
    return extract(text, as_of=AS_OF)


def ckd_epi_2021(scr_umol, age, sex):
    """Независимая реализация (sex: 1 = м, 0 = ж). Нужна, чтобы проверять C-ядро не им самим."""
    scr = scr_umol / 88.4
    k, a = (0.9, -0.302) if sex == 1 else (0.7, -0.241)
    v = 142.0 * (min(scr / k, 1.0) ** a) * (max(scr / k, 1.0) ** -1.200) * (0.9938 ** age)
    return v * (1.012 if sex == 0 else 1.0)


CLEAN_CONF = {k: True for k in (
    "no_epilepsy_or_organic_cns", "no_lactase_deficiency", "no_maoi_history", "no_pregnancy_lactation",
    "no_hepatic_insufficiency", "no_suicidal_ideation", "no_psychotic_symptoms")}

# =====================================================================================
print("\n=== A. Боевые кейсы из ревью ===")
base_tail = "Креатинин 80 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, GAD-7 = 12, п.9 = 0."

r = run("Женщина 40 лет. Диагноз F41.1. " + base_tail + " Эпилепсии нет.")
check("A1 «Эпилепсии нет» -> эпилепсия = False, а не True",
      r["extracted"]["profile"]["has_epilepsy_or_organic_cns"] is False)
check("A1 -> назначение не заблокировано ложно", r["rules_engine"]["status"] != "RED_HARD_STOP",
      r["rules_engine"]["status"])

r = run("Мужчина 45 лет. F41.1. ИМАО отменён 60 дней назад. " + base_tail)
p = r["extracted"]["profile"]
check("A2 «ИМАО отменён 60 дней назад» -> 60 суток, а не 7", p["days_since_maoi"] == 60, str(p["days_since_maoi"]))
check("A2 -> правило ИМАО не срабатывает",
      not any(f["rule_id"] == "AVIANDR_HS_04_MAOI" for f in r["rules_engine"]["findings"]))

r = run("Пациент жалуется на тревогу. F41.1.")
check("A3 пустая история -> возраст не выдуман", r["extracted"]["profile"]["age"] is None)
check("A3 -> не зелёный и не «рекомендовано»", r["overall_status"] == "AMBER_DATA_REQUIRED"
      and r["aviandr_decision_support"]["status"] == "DATA_REQUIRED", r["overall_status"])
check("A3 -> СКФ не посчитана из воздуха", r["c_core_pk_and_safety"]["egfr_ckd_epi"] is None)

r = run("Женщина 30 лет. F41.1. Креатинин 70 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 18, GAD-7 = 14.")
check("A4 п.9 не внесён -> не 0, а None", r["extracted"]["clinical_state"]["phq9_item9_suicidal"] is None)
check("A4 -> в пробелах «суицидальный риск не оценён»",
      any(g["field"] == "suicide_risk_assessment" for g in r["data_required"]))
r2 = run("Женщина 30 лет. F41.1. Креатинин 70 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 18, GAD-7 = 14.", CLEAN_CONF)
check("A4 -> даже при подтверждении всего остального без п.9 есть пробел (кроме явного подтверждения врача)",
      not any(g["field"] == "suicide_risk_assessment" for g in r2["data_required"]))  # no_suicidal_ideation подтверждён врачом
r3 = run("Женщина 30 лет. F41.1. Креатинин 70 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 18, GAD-7 = 14.",
         {k: v for k, v in CLEAN_CONF.items() if k != "no_suicidal_ideation"})
check("A4 -> без подтверждения суицидального риска зелёного нет",
      r3["overall_status"] != "GREEN_IN_LABEL" and any(g["field"] == "suicide_risk_assessment" for g in r3["data_required"]))

doc = json.dumps({"resourceType": "ClinicalImpression", "subject": {"age": 38, "gender": "female"},
                  "diagnosis_icd10": "F41.1",
                  "clinical_notes": "Непереносимость лактозы отрицает. Эпилепсии нет.",
                  "labs": {"creatinine_umol_l": 80, "child_pugh_class": "Чайлд-Пью: норма"},
                  "scales": {"PHQ9": 6, "PHQ9_item9": 0, "GAD7": 12, "CSSRS": 0}}, ensure_ascii=False)
e = ex(doc)
check("A5 JSON: «непереносимость лактозы отрицает» -> дефицит лактазы False", e["profile"]["has_lactase_deficiency"] is False)
check("A5 JSON: «эпилепсии нет» -> False", e["profile"]["has_epilepsy_or_organic_cns"] is False)
check("A5 JSON: «непереносимость» не делает из лактозы провал СИОЗС", e["clinical_state"]["failed_first_line_ssri"] is None)
check("A5 JSON: формат распознан, возраст/пол/шкалы на месте",
      e["format"] == "json" and e["profile"]["age"] == 38 and e["profile"]["sex"] == 0
      and e["clinical_state"]["phq9_item9_suicidal"] == 0)

r = run("Мужчина 45 лет. F41.1. Креатинин 1.4 мг/дл. Чайлд-Пью: норма. PHQ-9 = 6, GAD-7 = 12, п.9 = 0.")
check("A6 креатинин в мг/дл конвертирован в мкмоль/л", abs(r["extracted"]["profile"]["serum_creatinine_umol_l"] - 123.76) < 0.01)
check("A6 СКФ совпадает с независимой формулой",
      abs(r["c_core_pk_and_safety"]["egfr_ckd_epi"] - ckd_epi_2021(123.76, 45, 1)) < 0.15,
      f"{r['c_core_pk_and_safety']['egfr_ckd_epi']} vs {ckd_epi_2021(123.76, 45, 1):.2f}")

# =====================================================================================
print("\n=== B. Таблица фраз ===")


def field(text, name, bucket="profile"):
    return ex(text)[bucket][name]


EPI = "has_epilepsy_or_organic_cns"
NEG_TABLE = [
    ("Эпилепсии нет.", EPI, False), ("Без эпилепсии.", EPI, False), ("Эпилепсия: нет.", EPI, False),
    ("Эпилепсия в анамнезе.", EPI, True), ("Страдает эпилепсией.", EPI, True),
    ("Судорожные припадки в детстве.", EPI, True), ("Диагноз G40.2.", EPI, True),
    ("Не исключена эпилепсия.", EPI, None), ("Подозрение на эпилепсию.", EPI, None),
    ("Нет данных об эпилепсии.", EPI, None), ("Эпилепсия у матери.", EPI, None),
    ("Отрицает эпилепсию, ЧМТ, беременность.", EPI, False),
    ("Беременность отрицает, эпилепсия в анамнезе.", EPI, True),
    ("Эпилепсия, лактазная недостаточность — отрицает.", EPI, False),
    ("Непереносимость лактозы отрицает.", "has_lactase_deficiency", False),
    ("Дефицит лактазы.", "has_lactase_deficiency", True),
    ("Не беременна.", "is_pregnant_or_lactating", False), ("Беременна 12 недель.", "is_pregnant_or_lactating", True),
    ("Кормит грудью.", "is_pregnant_or_lactating", True), ("Лактацию отрицает.", "is_pregnant_or_lactating", False),
    ("Цирроз печени, класс С.", "hepatic_insufficiency_documented", True),
    ("Цирроза нет.", "hepatic_insufficiency_documented", False),
]
for text, fld, want in NEG_TABLE:
    got = field(text, fld)
    check(f"B «{text}» -> {fld} = {want}", got is want, f"получено {got}")

# ловушка: замыкающее отрицание относится к соседнему пункту — эпилепсию нельзя «отрицать» по соседству
got = field("Тревога, эпилепсия, беременность отрицает.", EPI)
check("B ловушка «…эпилепсия, беременность отрицает» -> эпилепсия НЕ False", got is not False, f"получено {got}")

MAOI = [
    ("Принимает моклобемид.", True, 0), ("Фенелзин 30 мг.", True, 0),
    ("ИМАО отменён 10 дней назад.", True, 10), ("ИМАО отменён 2 недели назад.", True, 14),
    ("ИМАО отменён три недели назад.", True, 21), ("Фенелзин отменён месяц назад.", True, 30),
    ("ИМАО отменён 60 дней назад.", True, 60), ("ИМАО принимал ранее.", True, None),
    ("ИМАО не принимает.", False, None), ("ИМАО отрицает.", False, None),
]
for text, ever, days in MAOI:
    pr = ex(text)["profile"]
    check(f"B ИМАО «{text}» -> ever={ever}, дней={days}", pr["maoi_ever"] is ever and pr["days_since_maoi"] == days,
          f"{pr['maoi_ever']}/{pr['days_since_maoi']}")

CREAT = "serum_creatinine_umol_l"
LAB_TABLE = [
    ("Креатинин 118 мкмоль/л.", CREAT, 118.0), ("Креатинин 1.4 мг/дл.", CREAT, 123.76),
    ("Креатинин 1,4 мг%.", CREAT, 123.76), ("Креат. 86.", CREAT, 86.0),
    ("Креатинин 1.4.", CREAT, None),                       # единицы не указаны, неоднозначно
    ("Креатинин 8000 мкмоль/л.", CREAT, None),             # неправдоподобно
    ("Креатинин 118 мкмоль/л (2019 г.).", CREAT, None),    # устарел
    ("Креатинин 118 мкмоль/л от 12.03.2019.", CREAT, None),
    ("Креатинин 118 мкмоль/л от 12.09.2026.", CREAT, 118.0),
    ("Креатинин 90 мкмоль/л (05.2019). Креатинин 120 мкмоль/л (09.2026).", CREAT, 120.0),
    ("Креатинин 90 мкмоль/л. Креатинин 150 мкмоль/л.", CREAT, None),   # конфликт без дат
    ("Билирубин 2.0 мг/дл.", "bilirubin_umol_l", 34.2), ("Билирубин 20 мкмоль/л.", "bilirubin_umol_l", 20.0),
    ("Альбумин 3.2 г/дл.", "albumin_g_l", 32.0), ("Альбумин 40 г/л.", "albumin_g_l", 40.0),
    ("МНО 1.8.", "inr", 1.8),
]
for text, fld, want in LAB_TABLE:
    got = field(text, fld)
    ok = (got is None and want is None) or (got is not None and want is not None and abs(got - want) < 0.05)
    check(f"B лаб. «{text}» -> {want}", ok, f"получено {got}")

check("B СКФ из ЭМК: «СКФ 52 мл/мин»", field("СКФ 52 мл/мин.", "egfr_reported") == 52.0)
check("B СКФ неравенством не используется", field("СКФ <60 мл/мин.", "egfr_reported") is None)

CP = [("Чайлд-Пью: класс B.", "B"), ("Чайлд-Пью: класс В.", "B"), ("Чайлд-Пью: норма.", "N"),
      ("Цирроз печени, класс С.", "C"), ("Печёночные пробы в норме.", "N"), ("Печень без особенностей.", None)]
for text, want in CP:
    got = field(text, "child_pugh_class_text")
    check(f"B Чайлд-Пью «{text}» -> {want}", got == want, f"получено {got}")

AGE = [("М, 36 г.", 36, 1), ("Женщина, 31 лет.", 31, 0), ("Пациентка 62 года.", 62, 0),
       ("Возраст: 45 лет.", 45, None), ("Креатинин 86 мкмоль/л от 12.03.19 г.", None, None),
       ("Диагноз F41.1, 2019 г.", None, None)]
for text, a, s_ in AGE:
    pr = ex(text)["profile"]
    check(f"B возраст/пол «{text}»", pr["age"] == a and (s_ is None or pr["sex"] == s_), f"{pr['age']}/{pr['sex']}")
check("B противоречивый пол -> None", field("Мужчина. Женщина.", "sex") is None)

SC = [("PHQ9 7б (п9=0)", "phq9_total", 7), ("PHQ9 7б (п9=0)", "phq9_item9_suicidal", 0),
      ("PHQ-9: 8 (вопрос №9: 0)", "phq9_total", 8), ("PHQ-9: 8 (вопрос №9: 0)", "phq9_item9_suicidal", 0),
      ("PHQ-9 = 45", "phq9_total", None), ("PHQ-9 = 18, п.9 = 2", "phq9_item9_suicidal", 2),
      ("ГТР-7 12б", "gad7_total", 12), ("GAD-7 = 15 б.", "gad7_total", 15),
      ("CSSRS=0", "cssrs_score", 0), ("C-SSRS: отрицательный", "cssrs_score", 0), ("C-SSRS = 4", "cssrs_score", 4),
      ("Жалобы на тревогу", "phq9_item9_suicidal", None)]
for text, fld, want in SC:
    got = field(text, fld, "clinical_state")
    check(f"B шкала «{text}» -> {fld} = {want}", got == want, f"получено {got}")
check("B «Суицидальных мыслей нет» -> False", field("Суицидальных мыслей нет.", "suicidal_ideation_documented", "clinical_state") is False)
check("B «Суицидальные мысли» -> True", field("Отмечает суицидальные мысли.", "suicidal_ideation_documented", "clinical_state") is True)

COM = [("Алкоголь не употребляет.", 64, False), ("Принимает диазепам.", 64, True),
       ("Принимает сертралин.", 32, True), ("Метопролол 50 мг.", 1, True), ("Эсциталопрам не принимает.", 32, False)]
for text, bit, present in COM:
    mask = field(text, "comedication_bitmask")
    check(f"B комедикация «{text}»", bool(mask & bit) is present, f"маска {mask}")

# доказательства: у каждой найденной сущности есть цитата, которая реально лежит в тексте
e = ex("Мужчина 45 лет. Эпилепсия в анамнезе. Креатинин 118 мкмоль/л. PHQ-9 = 6, п.9 = 0.")
ok = True
for fld_name, evs in e["evidence"].items():
    for ev_ in evs:
        if ev_.get("source") == "text" and ev_["text"].lower().strip() not in e["text_for_evidence"].lower().replace("\n", " "):
            ok = False
check("B evidence: цитаты найдены в исходном тексте", ok and "has_epilepsy_or_organic_cns" in e["evidence"])

# =====================================================================================
print("\n=== C. Сквозной пайплайн ===")
FULL = ("Мужчина 45 лет. Диагноз F41.1. Креатинин 80 мкмоль/л. Чайлд-Пью: норма. "
        "PHQ-9 = 6, GAD-7 = 12, п.9 = 0, C-SSRS = 0.")
r = run(FULL)
check("C без подтверждений врача -> AMBER_DATA_REQUIRED (эпилепсия/лактаза/ИМАО неизвестны)",
      r["overall_status"] == "AMBER_DATA_REQUIRED" and len(r["doctor_confirmations_needed"]) >= 3, r["overall_status"])
r = run(FULL, CLEAN_CONF)
check("C с подтверждениями врача -> GREEN_IN_LABEL и «рекомендовано в рамках ОХЛП»",
      r["overall_status"] == "GREEN_IN_LABEL" and r["aviandr_decision_support"]["status"] == "RECOMMENDED_IN_LABEL",
      f"{r['overall_status']} / {r['aviandr_decision_support']['status']}")
r = run(FULL + " Водит автомобиль.", CLEAN_CONF)
check("C водитель -> предупреждение, статус AMBER_REVIEW, но препарат in-label",
      r["overall_status"] == "AMBER_REVIEW" and r["aviandr_decision_support"]["status"] == "RECOMMENDED_IN_LABEL")
r = run("Диагноз F41.1.", CLEAN_CONF)
check("C подтверждения врача не создают цифры: нет возраста/СКФ -> всё равно DATA_REQUIRED",
      r["overall_status"] == "AMBER_DATA_REQUIRED"
      and {"age", "egfr_ckd_epi"} <= {g["field"] for g in r["data_required"]})
r = run("Женщина 40 лет. Эпилепсия. Диагноз F41.1.")
check("C противопоказание бьёт пробелы: эпилепсия + куча неизвестного -> RED_HARD_STOP",
      r["overall_status"] == "RED_HARD_STOP" and r["aviandr_decision_support"]["status"] == "HARD_STOP_CONTRAINDICATION")
check("C красный статус даёт альтернативы для врача", len(r["rules_engine"]["alternatives_if_blocked"]) > 0)
r = run("Женщина 42 года. F41.1. Креатинин 155 мкмоль/л. Лактазная недостаточность. PHQ-9 = 22, п.9 = 3, C-SSRS = 4, GAD-7 = 19.")
check("C сценарий-3: СКФ<60 + лактаза + C-SSRS=4 -> RED_CRITICAL_ALERT, оба блока видны",
      r["overall_status"] == "RED_CRITICAL_ALERT"
      and r["rules_engine"]["status"] == "RED_HARD_STOP"
      and r["routing_and_alerting"]["routing_category"] == "MANDATORY_PSYCHIATRIST")
r = run("Мужчина 45 лет. F41.1. Креатинин 118 мкмоль/л (2019 г.). Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.", CLEAN_CONF)
check("C устаревший креатинин -> СКФ неизвестна, зелёного нет, причина в warnings",
      r["overall_status"] == "AMBER_DATA_REQUIRED" and any("устарел" in w for w in r["warnings"]))
r = run("Мужчина 45 лет. F41.1. Креатинин 80 мкмоль/л. Чайлд-Пью: класс B. PHQ-9 = 6, п.9 = 0.", CLEAN_CONF)
check("C Чайлд-Пью B из текста -> RED_HARD_STOP", r["overall_status"] == "RED_HARD_STOP")
r = run("Мужчина 45 лет. Цирроз печени. F41.1. Креатинин 80 мкмоль/л. PHQ-9 = 6, п.9 = 0.", CLEAN_CONF)
check("C цирроз без класса -> RED (правило HS_06B)",
      any(f["rule_id"] == "AVIANDR_HS_06B_HEPATIC_DOCUMENTED" for f in r["rules_engine"]["findings"]))
r = run("Мужчина 45 лет. F41.1. Билирубин 60 мкмоль/л. Альбумин 28 г/л. МНО 1.9. Креатинин 80 мкмоль/л. PHQ-9 = 6, п.9 = 0.",
        CLEAN_CONF)
check("C класс Чайлд-Пью считает C-ядро из анализов -> A/B/C = красный",
      r["rules_engine"]["status"] == "RED_HARD_STOP" and r["c_core_pk_and_safety"]["child_pugh_class"] in "ABC")
check("C без асцита/энцефалопатии есть предупреждение о возможном занижении балла",
      any("асцит" in w.lower() for w in r["warnings"]))

# граница СКФ: берём креатинин по независимой формуле на обе стороны порога 60
lo = hi = None
for cr in range(60, 200):
    if ckd_epi_2021(cr, 50, 1) < 60.0 and lo is None:
        lo = cr
        hi = cr - 1
        break
below = run(f"Мужчина 50 лет. F41.1. Креатинин {lo} мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.", CLEAN_CONF)
above = run(f"Мужчина 50 лет. F41.1. Креатинин {hi} мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.", CLEAN_CONF)
check(f"C граница СКФ 60: креатинин {lo} -> красный, {hi} -> не красный",
      below["overall_status"] == "RED_HARD_STOP" and above["overall_status"] != "RED_HARD_STOP",
      f"{below['c_core_pk_and_safety']['egfr_ckd_epi']} / {above['c_core_pk_and_safety']['egfr_ckd_epi']}")

for days, red in ((14, True), (15, False)):
    r = run(f"Мужчина 45 лет. F41.1. ИМАО отменён {days} дней назад. Креатинин 80 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.",
            CLEAN_CONF)
    got = r["overall_status"] == "RED_HARD_STOP"
    check(f"C ИМАО {days} сут. -> {'красный' if red else 'не красный'}", got is red, r["overall_status"])

r = run("Женщина 31 год. F41.1. Креатинин 70 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.",
        {k: v for k, v in CLEAN_CONF.items() if k != "no_pregnancy_lactation"})
check("C женщина 31 год, беременность не упомянута -> пробел is_pregnant_or_lactating",
      any(g["field"] == "is_pregnant_or_lactating" for g in r["data_required"]))
r = run("Женщина 62 года. F41.1. Креатинин 70 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.",
        {k: v for k, v in CLEAN_CONF.items() if k != "no_pregnancy_lactation"})
check("C женщина 62 года -> беременность не требует подтверждения, но допущение выведено",
      not any(g["field"] == "is_pregnant_or_lactating" for g in r["data_required"]) and any("55" in a for a in r["assumptions"]))
r = run("Мужчина 40 лет. Эпилепсия в анамнезе. F41.1. Креатинин 70 мкмоль/л. Чайлд-Пью: норма. PHQ-9 = 6, п.9 = 0.", CLEAN_CONF)
check("C подтверждение врача не затирает найденную в ЭМК эпилепсию",
      r["extracted"]["profile"][EPI] is True and r["overall_status"] == "RED_HARD_STOP"
      and any("противоречит" in w for w in r["warnings"]))
r = run("Диагноз F41.1.")
check("C без PHQ-9/GAD-7 кластеризация не считается (вместо выдуманных значений)",
      r["cluster_24d_analysis"].get("status") == "NOT_COMPUTED_MISSING_INPUTS")
r = run(FULL, CLEAN_CONF)
check("C при расчёте кластера подставленные входы перечислены",
      "imputed_inputs" in r["cluster_24d_analysis"] and "moca" in r["cluster_24d_analysis"]["imputed_inputs"])
r = run("Мужчина 45 лет. CYP2D6*4/*4. " + FULL.split(". ", 1)[1], CLEAN_CONF)
check("C CYP2D6 PM из текста -> множитель экспозиции > 1", r["c_core_pk_and_safety"]["cyp2d6_exposure_multiplier"] > 1.0)
r = run(FULL, CLEAN_CONF)
check("C CYP2D6 не указан -> допущение выведено", any("CYP2D6 не указан" in a for a in r["assumptions"]))

# =====================================================================================
print("\n=== D. Свойства RulesEngine (fuzz, seed=7) ===")
rules = RulesEngine()
POSITIVE = {
    "age": 17, "is_pregnant_or_lactating": True, "has_epilepsy_or_organic_cns": True,
    "egfr_ckd_epi": 40.0, "child_pugh_class": "B", "has_lactase_deficiency": True,
    "hepatic_insufficiency_documented": True,
}
CLEAN = {"age": 45, "sex": 1, "egfr_ckd_epi": 95.0, "child_pugh_class": "N", "has_epilepsy_or_organic_cns": False,
         "has_lactase_deficiency": False, "maoi_ever": False, "days_since_maoi": None,
         "is_pregnant_or_lactating": False, "hepatic_insufficiency_documented": False,
         "comedication_bitmask": 0, "drives_vehicle": False}
check("D чистый профиль -> GREEN_IN_LABEL", rules.evaluate(CLEAN)["status"] == "GREEN_IN_LABEL")

REQUIRED = ["age", "sex", "egfr_ckd_epi", "child_pugh_class", "has_epilepsy_or_organic_cns",
            "has_lactase_deficiency", "maoi_ever"]
for f in REQUIRED:
    pr = dict(CLEAN)
    pr[f] = None
    if f == "sex":
        pr["is_pregnant_or_lactating"] = None   # при неизвестном поле пол беременность тоже требуется
    res = rules.evaluate(pr)
    check(f"D убрали {f} -> не зелёный, пробел назван",
          res["status"] != "GREEN_IN_LABEL" and any(g["field"] == f for g in res["data_required"]), res["status"])
pr = dict(CLEAN, sex=0, is_pregnant_or_lactating=None)
check("D женщина без данных о беременности -> пробел", any(g["field"] == "is_pregnant_or_lactating"
                                                          for g in rules.evaluate(pr)["data_required"]))
pr = dict(CLEAN, maoi_ever=True, days_since_maoi=None)
res = rules.evaluate(pr)
check("D ИМАО был, срок неизвестен -> пробел days_since_maoi, не красный",
      any(g["field"] == "days_since_maoi" for g in res["data_required"]) and res["status"] != "RED_HARD_STOP")
check("D бит-маска: седативы (64) не включают правило серотонинергических (32)",
      [f["rule_id"] for f in rules.evaluate(dict(CLEAN, comedication_bitmask=64))["findings"]] == ["AVIANDR_W_DDI_SEDATIVES"])

rnd = random.Random(7)
bad = 0
for _ in range(400):
    pr = dict(CLEAN)
    red_expected = False
    gap_expected = False
    for f in ["age", "sex", "egfr_ckd_epi", "child_pugh_class", "has_epilepsy_or_organic_cns", "has_lactase_deficiency",
              "is_pregnant_or_lactating", "hepatic_insufficiency_documented"]:
        c = rnd.random()
        if c < 0.18 and f in POSITIVE:
            pr[f] = POSITIVE[f]
        elif c < 0.38 and f != "hepatic_insufficiency_documented":
            pr[f] = None
    mode = rnd.random()
    if mode < 0.25:
        pr["maoi_ever"] = None
    elif mode < 0.45:
        pr["maoi_ever"] = True
        pr["days_since_maoi"] = rnd.choice([None, 0, 5, 14, 15, 60])
    if pr["sex"] is None:
        pr["sex"] = None
    res = rules.evaluate(pr)
    # оракул — независим от движка
    red_expected = (
        (pr["age"] is not None and pr["age"] < 18) or pr["is_pregnant_or_lactating"] is True
        or pr["has_epilepsy_or_organic_cns"] is True or pr["has_lactase_deficiency"] is True
        or (pr["maoi_ever"] is True and pr["days_since_maoi"] is not None and pr["days_since_maoi"] <= 14)
        or (pr["egfr_ckd_epi"] is not None and pr["egfr_ckd_epi"] < 60)
        or pr["child_pugh_class"] in ("A", "B", "C") or pr["hepatic_insufficiency_documented"] is True)
    gap_expected = (
        any(pr[k] is None for k in ("age", "sex", "egfr_ckd_epi", "child_pugh_class", "has_epilepsy_or_organic_cns",
                                    "has_lactase_deficiency", "maoi_ever"))
        or (pr["maoi_ever"] is True and pr["days_since_maoi"] is None)
        or (pr["sex"] != 1 and pr["is_pregnant_or_lactating"] is None))
    if red_expected:
        ok = res["status"] == "RED_HARD_STOP"
    elif gap_expected:
        ok = res["status"] in ("AMBER_DATA_REQUIRED", "AMBER_REVIEW")
    else:
        ok = res["status"] == "GREEN_IN_LABEL"
    if not ok:
        bad += 1
        if bad <= 3:
            print("   контрпример:", pr, "->", res["status"])
check("D инвариант на 400 случайных профилях: противопоказание = красный, пробел ≠ зелёный, иначе зелёный", bad == 0, f"нарушений {bad}")

# =====================================================================================
print("\n=== E. C-ядро: СКФ против независимой формулы ===")
worst = 0.0
n = 0
for sex in (0, 1):
    for age in range(18, 96, 7):
        for cr in range(40, 420, 20):
            c = engine.c_bridge.run_c_core({"age": age, "sex": sex, "serum_creatinine_umol_l": float(cr)}, fill_defaults=False)
            ref = ckd_epi_2021(cr, age, sex)
            worst = max(worst, abs(c["egfr_ckd_epi"] - ref))
            n += 1
check(f"E СКФ CKD-EPI 2021 на {n} точках: макс. расхождение {worst:.3f} мл/мин (допуск 0.06 — округление до 0.1)", worst <= 0.06)
c = engine.c_bridge.run_c_core({"age": 45, "sex": 1}, fill_defaults=False)
check("E без креатинина ядро не выдаёт СКФ", c["egfr_ckd_epi"] is None)
check("E без анализов печени класс Чайлд-Пью = «?»", c["child_pugh_class"] == "?")

# =====================================================================================
print("\n=== F. Маршрутизация: None-безопасность ===")
rt = engine.evaluate_routing_and_alerts({})
check("F пустое состояние -> DATA_REQUIRED, а не GREEN", rt["routing_category"] == "DATA_REQUIRED"
      and rt["ui_visual_block"]["color"] == "AMBER_WARNING", rt["routing_category"])
rt = engine.evaluate_routing_and_alerts({"phq9_total": 5, "phq9_item9_suicidal": 0, "cssrs_score": 0})
check("F явные нули -> прежний зелёный маршрут (обратная совместимость)", rt["routing_category"] == "PRIMARY_CARE_MANAGEMENT")
rt = engine.evaluate_routing_and_alerts({"phq9_total": 18, "phq9_item9_suicidal": None})
check("F PHQ-9=18 без п.9 -> не зелёный, пробел назван",
      rt["ui_visual_block"]["color"] != "GREEN_SAFE" and rt["data_gaps"])
rt = engine.evaluate_routing_and_alerts({"suicidal_ideation_documented": True})
check("F суицидальные мысли в ЭМК -> MANDATORY + критический алерт",
      rt["routing_category"] == "MANDATORY_PSYCHIATRIST" and rt["critical_alert_triggered"])
rt = engine.evaluate_routing_and_alerts({"suicidal_ideation_documented": False, "phq9_total": 4})
check("F «мыслей нет» в ЭМК = риск оценён -> нет пробела", not rt["data_gaps"])
rt = engine.evaluate_routing_and_alerts({"phq9_total": 6, "phq9_item9_suicidal": 1, "cssrs_score": 0})
check("F п.9 >= 1 -> MANDATORY (поведение не изменилось)", rt["routing_category"] == "MANDATORY_PSYCHIATRIST")
rt = engine.evaluate_routing_and_alerts({"phq9_total": 6, "phq9_item9_suicidal": 0, "cssrs_score": 4})
check("F C-SSRS >= 4 -> критический алерт", rt["critical_alert_triggered"])

# =====================================================================================
print("\n=== G. Чек-ин пациента ===")
try:
    import uuid
    from api_alerts import CheckinRequest, _is_critical, _suicide_item_missing
    c0 = CheckinRequest(event_uuid=uuid.uuid4(), phq9_total=10)
    check("G пропущенные п.9 и C-SSRS -> None, а не 0", c0.phq9_item9 is None and c0.cssrs is None)
    check("G пропуск помечен для PWA (повторно задать вопрос)", _suicide_item_missing(c0) is True)
    check("G пропуск не создаёт ложный критический алерт", _is_critical(c0) is False)
    c1 = CheckinRequest(event_uuid=uuid.uuid4(), phq9_total=10, phq9_item9=2)
    check("G п.9 = 2 -> критический, пропуска нет", _is_critical(c1) is True and _suicide_item_missing(c1) is False)
    c2 = CheckinRequest(event_uuid=uuid.uuid4(), phq9_total=10, phq9_item9=0, cssrs=0)
    check("G явные нули -> не критический, пропуска нет", _is_critical(c2) is False and _suicide_item_missing(c2) is False)
except Exception as exc:  # noqa: BLE001
    SKIPPED.append(f"G ({type(exc).__name__}: {exc})")
    print(f"[SKIP] G чек-ин: {type(exc).__name__}: {exc}")

# =====================================================================================
print("\n=== H. Синтетический корпус (100 виньеток) ===")
corpus_path = ROOT / "clinical_data" / "synthetic_100_ehr_vignettes.json"
if corpus_path.exists():
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    miss_block, miss_mand, stats = [], [], {}
    for c in corpus:
        res = run(c["raw_text"])
        stats[res["overall_status"]] = stats.get(res["overall_status"], 0) + 1
        if c["gt_is_aviandr_blocked"] and res["overall_status"] not in ("RED_HARD_STOP", "RED_CRITICAL_ALERT"):
            miss_block.append(c["case_id"] if "case_id" in c else c.get("template_type"))
        if c["gt_routing_category"] == "MANDATORY_PSYCHIATRIST" and \
                res["routing_and_alerting"]["routing_category"] != "MANDATORY_PSYCHIATRIST":
            miss_mand.append(c.get("case_id", c.get("template_type")))
        if (c["gt_routing_category"] != "MANDATORY_PSYCHIATRIST"
                and res["overall_status"] == "GREEN_IN_LABEL"):
            miss_block.append("GREEN_без_подтверждений:" + str(c.get("case_id")))
    check(f"H ни один gt-заблокированный случай не получил не-красный статус ({sum(1 for c in corpus if c['gt_is_aviandr_blocked'])} шт.)",
          not miss_block, str(miss_block[:5]))
    check("H все gt-обязательные направления к психиатру найдены (recall = 100%)", not miss_mand, str(miss_mand[:5]))
    check("H без подтверждений врача корпус не даёт ни одного GREEN (в тексте нет данных об эпилепсии/ИМАО/лактазе)",
          stats.get("GREEN_IN_LABEL", 0) == 0, str(stats))
    print("   распределение статусов на корпусе:", stats)
else:
    SKIPPED.append("H (корпус не найден)")
    print("[SKIP] H корпус не найден")

# =====================================================================================
print("\n=== I. HTTP-эндпоинт ===")
try:
    # Клинический API требует Bearer (долг безопасности, 7afac24): берём полный app,
    # логинимся демо-врачом и ходим с заголовком — как это делает настоящий фронтенд.
    import atexit  # noqa: E402
    from contextlib import ExitStack  # noqa: E402
    from fastapi.testclient import TestClient as _TC  # noqa: E402
    from main import app as _full_app  # noqa: E402

    _stack = ExitStack()
    client = _stack.enter_context(_TC(_full_app))
    atexit.register(_stack.close)
    _tok = client.post("/api/v1/auth/login", json={
        "login": "neuro.ivlanov", "password": "compass-demo-2026"}).json()["access_token"]
    _H = {"Authorization": f"Bearer {_tok}"}
    url = "/api/v1/compass-bio/ehr/analyze-and-route"

    resp = client.post(url, headers=_H, json={"raw_ehr_text": "Пациент жалуется на тревогу. F41.1."})
    j = resp.json()
    check("I пустая история через API -> 200 и AMBER_DATA_REQUIRED", resp.status_code == 200
          and j["overall_status"] == "AMBER_DATA_REQUIRED", f"{resp.status_code} {str(j)[:200]}")
    check("I API: без шкал нет 24D-вектора; XAI/CBR = честный null",
          "state_vector_24d" not in j["compass_decision"]["cluster_24d_analysis"]
          and j["xai_gost_59921_shap"] is None and j["case_based_reasoning_rki_cohorts"] is None)
    resp = client.post(url, headers=_H, json={"raw_ehr_text": FULL, "doctor_confirmations": CLEAN_CONF})
    j = resp.json()
    check("I API: полный случай + подтверждения -> GREEN_IN_LABEL, XAI/CBR на месте",
          resp.status_code == 200 and j["overall_status"] == "GREEN_IN_LABEL"
          and j["xai_gost_59921_shap"] is not None, f"{resp.status_code} {j.get('overall_status')}")
    resp = client.post(url, headers=_H, json={"raw_ehr_text": "Женщина 40 лет. Эпилепсии нет. " + base_tail})
    check("I API: «эпилепсии нет» не даёт ложного красного", resp.json()["compass_decision"]["rules_engine"]["status"] != "RED_HARD_STOP")
except Exception as exc:  # noqa: BLE001
    SKIPPED.append(f"I ({type(exc).__name__}: {exc})")
    print(f"[SKIP] I API: {type(exc).__name__}: {exc}")

# =====================================================================================
print()
if SKIPPED:
    print("Пропущено:", "; ".join(SKIPPED))
if FAILURES:
    print(f"[FAIL] Упало проверок: {len(FAILURES)}")
    for f in FAILURES:
        print("   -", f)
    sys.exit(1)
print("[OK] Все проверки безопасного разбора пройдены.")
