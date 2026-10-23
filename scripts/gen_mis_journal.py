# -*- coding: utf-8 -*-
"""
scripts/gen_mis_journal.py — генератор «журнала приёма на сегодня» для МИС-стенда
(backend_emc/data/patients_today.json — «БД» вендорского сервера ЭМК).

Источники (все пациенты синтетические/обезличенные):
  1. clinical_data/webinar_patient_profiles.json — 5 персон с вебинара организаторов
     (факты персоны со слайдов; лаборатория/шкалы — синтетическое дополнение, см. поля
     synthetic_completion в самом файле);
  2. benchmarks/holdout/cases.json — 24 независимых контрольных случая;
  3. clinical_data/synthetic_100_ehr_vignettes.json — корпус 100 (берём выборку).

Детерминизм: имена/ОМС/время генерируются из hash(case_id) — при повторных запусках
журнал одинаковый (демо не «дышит» между прогонами).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SURNAMES_M = ["Абрамов", "Белов", "Воронов", "Гаврилов", "Демидов", "Ефимов", "Захаров",
              "Ильин", "Ковалёв", "Лебедев", "Морозов", "Никитин", "Осипов", "Павлов"]
SURNAMES_F = ["Аксёнова", "Богданова", "Волкова", "Гусева", "Дмитриева", "Ершова",
              "Журавлёва", "Зайцева", "Иванова", "Кузнецова", "Лазарева", "Макарова"]
NAMES_M = ["Александр", "Борис", "Виктор", "Григорий", "Денис", "Евгений", "Игорь", "Кирилл"]
NAMES_F = ["Анна", "Вера", "Галина", "Дарья", "Екатерина", "Ирина", "Ксения", "Людмила"]
PATR_M = ["Александрович", "Борисович", "Викторович", "Сергеевич", "Николаевич", "Андреевич"]
PATR_F = ["Александровна", "Борисовна", "Викторовна", "Сергеевна", "Николаевна", "Андреевна"]


def _rng(case_id: str) -> random.Random:
    return random.Random(int(hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12], 16))


def _synth_identity(case_id: str, sex: str) -> dict:
    r = _rng(case_id)
    male = sex != "Ж"
    surname = r.choice(SURNAMES_M if male else SURNAMES_F)
    name = r.choice(NAMES_M if male else NAMES_F)
    patr = r.choice(PATR_M if male else PATR_F)
    year = r.randint(1962, 2004)
    return {
        "fio": f"{surname} {name} {patr}",
        "sex": "М" if male else "Ж",
        "born": f"{year:04d}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
        "oms": f"7700 {r.randint(1000, 9999)} {r.randint(1000, 9999)} {r.randint(1000, 9999)}",
    }


def _sex_age_from_text(text: str):
    m = re.search(r"(Мужчина|Женщина)[^\d]{0,12}(\d{1,3})", text)
    if m:
        return ("М" if m.group(1) == "Мужчина" else "Ж"), int(m.group(2))
    m = re.search(r"\"gender\"\s*:\s*\"(male|female)\".*?\"age\"\s*:\s*(\d+)", text)
    if m:
        return ("М" if m.group(1) == "male" else "Ж"), int(m.group(2))
    return None, None


def build_journal() -> list[dict]:
    rows: list[dict] = []

    # --- 1. вебинарные персоны (приём начинается с них — сценарии демо)
    webinar = json.loads((ROOT / "clinical_data" / "webinar_patient_profiles.json").read_text("utf-8"))
    for p in webinar:
        rows.append({
            "case_ref": p["case_id"],
            "source": "webinar",
            "tag": p["mis"]["tag"],
            "fio": p["mis"]["fio"], "sex": p["mis"]["sex"], "born": p["mis"]["born"],
            "oms": p["mis"]["oms"], "icd10": p["expected_icd10"],
            "doctor": p["mis"]["doctor"],
            "raw_text": p["raw_text"],
            "demo_hint": p["demo_hint"],
        })

    # --- 2. holdout (контрольные случаи команды — все прогнаны, 0 опасных ошибок)
    holdout = json.loads((ROOT / "benchmarks" / "holdout" / "cases.json").read_text("utf-8"))
    for c in holdout:
        sex, age = _sex_age_from_text(c["raw_text"])
        ident = _synth_identity(c["case_id"], sex or "М")
        rows.append({
            "case_ref": c["case_id"],
            "source": "holdout",
            "tag": f"Контроль: {c['cohort']}",
            "fio": ident["fio"], "sex": ident["sex"] or sex, "born": ident["born"],
            "oms": ident["oms"], "icd10": c.get("expected_icd10") or c.get("gt_icd10") or "",
            "doctor": "невролог",
            "raw_text": c["raw_text"],
            "demo_hint": "",
        })

    # --- 3. выборка из корпуса 100 (обычная поликлиническая нагрузка)
    corpus = json.loads((ROOT / "clinical_data" / "synthetic_100_ehr_vignettes.json").read_text("utf-8"))
    for c in corpus[::11][:9]:  # каждый 11-й — 9 штук, детерминированно
        cid = f"SYN-{c['case_id']}"
        sex, age = _sex_age_from_text(c["raw_text"])
        ident = _synth_identity(cid, sex or "М")
        rows.append({
            "case_ref": cid,
            "source": "corpus",
            "tag": "Приём по направлению",
            "fio": ident["fio"], "sex": ident["sex"], "born": ident["born"],
            "oms": ident["oms"], "icd10": c.get("gt_icd10") or "",
            "doctor": "терапевт",
            "raw_text": c["raw_text"],
            "demo_hint": "",
        })

    # --- хронология демо: утро — 10 законченных приемов (holdout), в 09:20 «на приёме»
    # вебинарная персона №1 (живой сценарий демо), дальше вебинарные и остальные «ожидает».
    rows = rows[5:15] + rows[0:5] + rows[15:]

    minute = 8 * 60
    live_idx = 10  # WEB-01 после 10 законченных утренних
    for i, row in enumerate(rows):
        hh, mm = divmod(minute, 60)
        row["slot"] = f"{hh:02d}:{mm:02d}"
        row["ib"] = f"{4101 + i}/26"
        if i < live_idx:
            row["status"] = "закончен"
        elif i == live_idx:
            row["status"] = "на приёме"
        else:
            row["status"] = "ожидает"
        minute += 8
    return rows


def main() -> None:
    journal = build_journal()
    out = ROOT / "backend_emc" / "data" / "patients_today.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(journal, ensure_ascii=False, indent=1), "utf-8")
    by_src = {}
    for r in journal:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print(f"[gen_mis_journal] {len(journal)} записей -> {out.relative_to(ROOT)} | {by_src}")


if __name__ == "__main__":
    main()
