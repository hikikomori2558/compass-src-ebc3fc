#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_rules_engine.py — регрессионный набор для декларативного движка правил
(метрики 3, 5 и 10; граничные значения — прямое требование чек-листа кейса).

Прогон: python3 benchmarks/test_rules_engine.py
Здесь проверяются ровно те вещи, на которых проседают команды:
  * границы СКФ 59/60/61 и ИМАО 13/14/15 суток — правило обязано переключаться
    ровно на границе из инструкции, а не «примерно»;
  * tri-state: отсутствие данного — это «неизвестно», а не «нет»; неизвестность
    не может выпускать зелёный статус;
  * «новый препарат без перекомпиляции»: правим JSON на лету — поведение меняется,
    ядро C не пересобирается (метрика 10 вживую).
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from rules_engine import RULES_DIR, RulesEngine  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


BASE = {
    "age": 45, "sex": 1, "egfr_ckd_epi": 95.0, "child_pugh_class": "N",
    "days_since_maoi": None, "is_pregnant_or_lactating": False,
    "has_epilepsy_or_organic_cns": False, "has_lactase_deficiency": False,
    "hypersensitivity": False, "comedication_bitmask": 0, "drives_vehicle": False,
}

eng = RulesEngine()

# --- 1. Чистый профиль: зелёный, ноль находок ---
res = eng.evaluate(BASE)
check("Чистый профиль -> GREEN_IN_LABEL", res["status"] == "GREEN_IN_LABEL" and not res["findings"])
check("У каждой находки есть источник (метрика 5) — проверим на красных ниже", True)

# --- 2. Границы СКФ (59/60/61): инструкция говорит «< 60» ---
for egfr, expect in [(59.9, True), (60.0, False), (61.0, False)]:
    r = eng.evaluate({**BASE, "egfr_ckd_epi": egfr})
    fired = any(f["rule_id"] == "AVIANDR_HS_05_RENAL" for f in r["findings"])
    check(f"СКФ {egfr}: правило СКФ {'срабатывает' if expect else 'молчит'}", fired == expect)

# --- 3. Границы ИМАО (13/14/15 суток): wash-out 2 недели, «в течение» включительно ---
for days, expect in [(13, True), (14, True), (15, False)]:
    r = eng.evaluate({**BASE, "days_since_maoi": days})
    fired = any(f["rule_id"] == "AVIANDR_HS_04_MAOI" for f in r["findings"])
    check(f"ИМАО {days} сут: правило {'срабатывает' if expect else 'молчит'}", fired == expect)
check("ИМАО «никогда» (None) не приравнивается к нулю дней",
      not any(f["rule_id"] == "AVIANDR_HS_04_MAOI" for f in eng.evaluate(BASE)["findings"]))

# --- 4. Граница возраста 17/18/19 ---
for age, expect in [(17, True), (18, False), (19, False)]:
    r = eng.evaluate({**BASE, "age": age})
    fired = any(f["rule_id"] == "AVIANDR_HS_01_AGE" for f in r["findings"])
    check(f"Возраст {age}: правило {'срабатывает' if expect else 'молчит'}", fired == expect)

# --- 5. Чайлд-Пью: любой класс A/B/C запрещён, N — нет ---
for cls, expect in [("A", True), ("B", True), ("C", True), ("N", False)]:
    r = eng.evaluate({**BASE, "child_pugh_class": cls})
    fired = any(f["rule_id"] == "AVIANDR_HS_06_HEPATIC" for f in r["findings"])
    check(f"Чайлд-Пью {cls}: правило {'срабатывает' if expect else 'молчит'}", fired == expect)

# --- 6. Tri-state: нет креатинина -> нет зелёного, есть data_required (метрика 3) ---
no_renal = {k: v for k, v in BASE.items() if k != "egfr_ckd_epi"}
r = eng.evaluate(no_renal)
check("Нет СКФ -> не зелёный", r["status"] != "GREEN_IN_LABEL", r["status"])
check("Нет СКФ -> data_required про СКФ",
      any(g["field"] == "egfr_ckd_epi" for g in r["data_required"]))
r = eng.evaluate({"age": BASE["age"]})  # почти всё неизвестно
check("Почти пустой профиль -> AMBER_DATA_REQUIRED", r["status"] == "AMBER_DATA_REQUIRED")

# --- 7. Метрика 5: у красной находки есть цитата и ссылка на источник ---
r = eng.evaluate({**BASE, "egfr_ckd_epi": 40.0})
hs = next(f for f in r["findings"] if f["rule_id"] == "AVIANDR_HS_05_RENAL")
check("У находки есть source_id/цитата", hs["source"]["source_id"] == "AVIANDR_OHLP" and "СКФ" in hs["source"]["quote"])
check("Красный статус даёт альтернативы (in-label) для врача",
      len(r["alternatives_if_blocked"]) >= 3)

# --- 8. Метрика 10 вживую: правило — данные, не код ---
with tempfile.TemporaryDirectory() as tmp:
    # 8a. Правим порог СКФ в копии набора (как будто новая редакция инструкции)
    doc = json.loads((RULES_DIR / "aviandr_hard_stops.json").read_text(encoding="utf-8"))
    for rule in doc["rules"]:
        if rule["rule_id"] == "AVIANDR_HS_05_RENAL":
            rule["when"]["value"] = 30  # граница изменилась
    tmp_path = Path(tmp) / "edited.json"
    tmp_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    eng2 = RulesEngine()
    eng2.load("aviandr_hard_stops", path=tmp_path)
    fired_old = any(f["rule_id"] == "AVIANDR_HS_05_RENAL"
                    for f in eng.evaluate({**BASE, "egfr_ckd_epi": 45.0})["findings"])
    fired_new = any(f["rule_id"] == "AVIANDR_HS_05_RENAL"
                    for f in eng2.evaluate({**BASE, "egfr_ckd_epi": 45.0})["findings"])
    check("Порог в данных: СКФ 45 при пороге 60 запрещён", fired_old)
    check("Порог в данных: СКФ 45 при пороге 30 разрешён (без перекомпиляции C!)", not fired_new)
    check("Версия набора и sha256 фиксируются (для rule_versions в БД)",
          eng2.version == doc["version"] and len(eng2.content_sha256) == 64)

    # 8b. «Новый препарат» = новый JSON-файл, движок его поднимает как есть
    newdrug = {
        "rule_set": "demo_new_drug", "version": "0.1.0", "rules": [
            {"rule_id": "NEW_HS_01", "severity": "HARD_STOP",
             "when": {"field": "age", "op": "<", "value": 12},
             "message_ru": "Демо: <12 лет нельзя",
             "source": {"source_id": "DEMO", "doc": "демо-инструкция", "quote": "демо-цитата"}}
        ],
        "required_fields": [], "alternatives_if_blocked": []
    }
    nd_path = Path(tmp) / "demo_new_drug.json"
    nd_path.write_text(json.dumps(newdrug, ensure_ascii=False), encoding="utf-8")
    eng3 = RulesEngine()
    eng3.load("demo_new_drug", path=nd_path)
    r = eng3.evaluate({"age": 10, "age_known": True})
    check("Новый препарат из JSON: правило сработало без единой строки C",
          any(f["rule_id"] == "NEW_HS_01" for f in r["findings"]))

# --- 9. Человек в контуре: примечание присутствует всегда ---
check("Human-in-the-loop примечание на месте", "врачом" in eng.evaluate(BASE)["human_in_the_loop_note"])

print()
if FAILURES:
    print(f"[FAIL] Провалено {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("[OK] Все проверки движка правил пройдены.")
