# -*- coding: utf-8 -*-
"""
scripts/gen_emc_from_personas.py — журнал ЭМК из 105 рукописных персон.

Суть демо: врач работает в ЭМК вендора, COMPASS-закладка считывает карту с экрана.
Значит КАЖДЫЙ из 105 пациентов обязан иметь карту в ЭМК — с ИБ, ИМС-полисом,
слотом приёма и сырым текстом (его парсит Bitap-NER движка: МКБ, креатинин,
шкалы, коморбидность). Персонажи — из synthetic_personas.py; здесь только
детерминированный рендер карт.

Запуск: python3 -m scripts.gen_emc_from_personas   (локально, файл коммитится)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from synthetic_personas import PERSONAS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "backend_emc" / "data" / "patients_today.json"

M_NAMES = ["Александр", "Дмитрий", "Сергей", "Андрей", "Михаил", "Игорь", "Павел", "Роман",
           "Виктор", "Олег", "Николай", "Владимир", "Кирилл", "Тимофей", "Артём", "Глеб"]
M_PART = ["Александрович", "Дмитриевич", "Сергеевич", "Игоревич", "Петрович", "Николаевич",
          "Викторович", "Олегович"]
F_NAMES = ["Елена", "Ольга", "Наталья", "Ирина", "Татьяна", "Анна", "Мария", "Светлана",
           "Екатерина", "Юлия", "Ксения", "Анастасия", "Дарья", "Вера", "Полина", "Людмила"]
F_PART = ["Александровна", "Дмитриевна", "Сергеевна", "Игоревна", "Петровна", "Николаевна",
          "Викторовна", "Олеговна"]

COMPLAINTS_BY_GAD = {
    (0, 5): "жалоб почти нет, отмечает улучшение самочувствия",
    (6, 9): "периодическая тревожность, сон удовлетворительный",
    (10, 13): "тревожность и внутреннее напряжение, трудности засыпания",
    (14, 17): "постоянная тревога, раздражительность, мышечное напряжение",
    (18, 99): "выраженная тревога с соматикой (сердцебиение, дрожь), нарушения сна",
}


def fio(p: dict) -> str:
    n = p["n"]
    surname = p["l"].split(" ")[1]          # «С-001 Крылова А., …» → Крылова
    names = F_NAMES if p["s"] == 0 else M_NAMES
    parts = F_PART if p["s"] == 0 else M_PART
    return f"{surname} {names[(n * 3) % len(names)]} {parts[(n * 5) % len(parts)]}"


def oms(n: int) -> str:
    d = f"{4700 + n % 300}{n % 10000:04d}{(n * 7919) % 10000:04d}{(n * 31) % 100:02d}"
    return f"{d[0:4]} {d[4:8]} {d[8:12]} {d[12:16]}"


def raw_text(p: dict) -> str:
    n, age = p["n"], 2026 - p["b"]
    gad = p["gad"][-1]
    sex_word = "Мужчина" if p["s"] == 1 else "Женщина"
    for (lo, hi), c in COMPLAINTS_BY_GAD.items():
        if lo <= gad <= hi:
            complaints = c
            break
    creat = "152 мкмоль/л (ХБП С4, СКФ 22 — наблюдается нефрологом)" if n == 50 \
        else f"{62 + (n * 13) % 50} мкмоль/л"
    extra = []
    if n in (2, 15, 79, 105):
        extra.append("Работает водителем — вопрос о вождении.")
    if n in (8, 13, 94):
        extra.append("Гипертония, получает антигипертензивную терапию.")
    if n == 50:
        extra.append("Назначение по решению ВК: доза 20 мг/сут, креатинин 1 р/нед.")
    if n >= 98:
        extra.append(f"PHQ-9 = {20 + n % 4} (п.9 = 1), C-SSRS = {2 + n % 3}: "
                     "требуется консультация психиатра.")
    phq = f"{20 + n % 4}" if n >= 98 else f"{3 + (n % 4)}"
    weeks = p["d"] // 7
    return (f"Амбулаторный приём. {sex_word}, {age} лет. Диагноз: {p['icd']}. "
            f"На терапии Авиандр 20 мг 2 р/сут {weeks} нед. Жалобы: {complaints}. "
            f"Креатинин сыворотки: {creat}. Печёночные пробы в норме. "
            f"Шкалы: GAD-7 = {gad}, PHQ-9 = {phq}"
            + (" (п.9 = 1)" if n >= 98 else " (п.9 = 0)")
            + ("." + (" " + " ".join(extra)) if extra else ""))


def main() -> int:
    records = []
    for i, p in enumerate(PERSONAS):
        n = p["n"]
        hint = ""
        if "ood" in p["f"]:
            hint = "Аномальный профиль: система честно пометит OOD (без экстраполяции)"
        elif "crit-unconf" in p["f"]:
            hint = "Кризис: COMPASS покажет красный баннер ОБЯЗАТЕЛЬНО к психиатру"
        elif "override-ckd" in p["f"]:
            hint = "Жёсткий стоп по СКФ + осознанный оверрайд ВК в аудите"
        elif any(x.startswith("drop") for x in p["f"]):
            hint = "Дропаут: перестал отмечать приёмы — видно по стрику"
        records.append({
            "case_ref": f"SYN-{n:03d}",
            "source": "synthetic",
            "tag": f"Синтетический пациент стенда · {p['l']}",
            "fio": fio(p),
            "sex": "М" if p["s"] == 1 else "Ж",
            "born": f"{p['b']}-{(n % 12) + 1:02d}-{(n % 27) + 1:02d}",
            "oms": oms(n),
            "icd10": p["icd"],
            "doctor": "психиатр" if n >= 98 else "невролог",
            "raw_text": raw_text(p),
            "demo_hint": hint,
            "slot": f"{8 + (i * 17) // 60:02d}:{(i * 17) % 60:02d}",
            "ib": f"41{n:02d}/26",
            "status": "закончен" if i < 96 else "приём",
        })
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[gen_emc_from_personas] {len(records)} карт -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
