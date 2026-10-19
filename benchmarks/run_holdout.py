#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон hold-out (см. benchmarks/holdout/README.md). Печатает опасные ошибки отдельно от остальных."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from ehr_extract import extract  # noqa: E402

path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "benchmarks" / "holdout" / "cases.json"
if not path.exists():
    sys.exit(f"Нет {path}. Образец формата: benchmarks/holdout/cases.example.json (случаи пишет НЕ автор парсера).")
cases = json.loads(path.read_text(encoding="utf-8"))
as_of = date(2026, 10, 18)


def same(got, want):
    if got is None or want is None:
        return got is want
    if isinstance(want, bool) or isinstance(got, bool):
        return got is want
    return abs(float(got) - float(want)) < 0.5


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
