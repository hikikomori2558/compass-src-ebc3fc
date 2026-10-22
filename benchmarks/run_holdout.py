#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон hold-out (см. benchmarks/holdout/README.md). Печатает опасные ошибки отдельно от остальных."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # package-импорты backend.* из любого cwd

from backend.services.ehr_extract import extract  # noqa: E402

path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "benchmarks" / "holdout" / "cases.json"
if not path.exists():
    sys.exit(f"Нет {path}. Формат кейсов описан в benchmarks/holdout/README.md.")
cases = json.loads(path.read_text(encoding="utf-8"))
as_of = date(2026, 10, 18)


def same(got, want):
    if got is None or want is None:
        return got is want
    if isinstance(want, bool) or isinstance(got, bool):
        return got is want
    return abs(float(got) - float(want)) < 0.5


# Два формата кейсов, auto-detect:
#  A) {id, text, expected:{field:value}}      — полевой формат (field-level, автор парсера отделён)
#  B) {case_id, raw_text, expected_blocked, expected_routing, expected_missing_labs}
#     — pipeline-формат (ground truth уровня маршрутизации; safe_pipeline, не парсер)
pipeline_cases = [c for c in cases if "expected_routing" in c]
field_cases = [c for c in cases if "expected" in c]
if pipeline_cases and not field_cases:
    from backend.domain.routing import ClinicalCompassRuleEngine
    from backend.services.pipeline import SafeCompassPipeline

    pipeline = SafeCompassPipeline(ClinicalCompassRuleEngine())
    dangerous, false_alarm, missed = [], [], []
    ok = total = 0
    for c in pipeline_cases:
        res = pipeline.analyze_text(c["raw_text"])
        pred_blocked = res["rules_engine"]["status"] == "RED_HARD_STOP"
        pred_routing = res["routing_and_alerting"]["routing_category"]
        pred_missing = any("МЕТРИКА №3" in w for w in
                           res["c_core_pk_and_safety"]["endocrine_and_somatic_masks"])

        for label, pred, want in (
            ("blocked", pred_blocked, c["expected_blocked"]),
            ("routing", pred_routing, c["expected_routing"]),
            ("missing_labs", pred_missing, c["expected_missing_labs"]),
        ):
            total += 1
            if pred == want:
                ok += 1
            elif label == "blocked" and want and not pred:
                dangerous.append((c["case_id"], label, want, pred))  # противопоказание прошло зелёным
            elif label == "blocked" and pred and not want:
                false_alarm.append((c["case_id"], label, want, pred))
            else:
                missed.append((c["case_id"], label, want, pred))
    print(f"Режим: pipeline-level (формат Михаила). Случаев: {len(pipeline_cases)}, проверок: {total}")
    print(f"Совпало: {ok}/{total}")
    print(f"ОПАСНЫХ ошибок (противопоказание прошло без красного): {len(dangerous)}")
    for d in dangerous:
        print("   ", d)
    print(f"Прочих расхождений: ложная тревога {len(false_alarm)}, пропуск {len(missed)}")
    for d in false_alarm + missed:
        print("   ", d)
    sys.exit(1 if dangerous else 0)


dangerous, false_alarm, missed, ok, total = [], [], [], 0, 0
for c in cases:
    e = extract(c["text"], as_of=as_of)
    flat = {**e["profile"], **e["clinical_state"]}
    for fld, want in c["expected"].items():
        total += 1
        got = flat.get(fld)
        if same(got, want):
            ok += 1
        elif got is False and want is not False:
            dangerous.append((c["id"], fld, want, got))          # ожидали да/неизвестно, получили «нет»
        elif want is False and got is not False:
            false_alarm.append((c["id"], fld, want, got))        # безопасная ошибка: уйдёт врачу как «подтвердите»
        else:
            missed.append((c["id"], fld, want, got))
print(f"Случаев: {len(cases)}, проверок полей: {total}")
print(f"Совпало: {ok}/{total}")
print(f"ОПАСНЫХ ошибок (ожидали да/неизвестно, получили «нет»): {len(dangerous)}")
for d in dangerous:
    print("   ", d)
print(f"Безопасных ошибок: ложная тревога {len(false_alarm)}, пропуск значения {len(missed)}")
for d in false_alarm + missed:
    print("   ", d)
sys.exit(1 if dangerous else 0)
