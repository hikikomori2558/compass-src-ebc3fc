#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pii_scrubber.py — тесты и бенчмарк обезличивания ПДн (Метрика №13, 152-ФЗ).
Запуск: python3 benchmarks/test_pii_scrubber.py  (или pytest benchmarks/test_pii_scrubber.py)
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pii_scrubber import PiiScrubber  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


S = PiiScrubber(pepper="test_pepper")

# --- 1. Телефоны во всех форматах ---
for phone in ["+7 (913) 123-45-67", "+79131234567", "89131234567", "8 913 123 45 67", "7-913-123-45-67"]:
    clean, mask, kinds = S.scrub(f"Позвонить по тел. {phone} завтра.")
    check(f"Телефон замаскирован: {phone}", "PII_" in clean and (mask & 1), clean.strip())

# --- 2. СНИЛС ---
clean, mask, kinds = S.scrub("СНИЛС 123-456-789 00 предъявлен.")
check("СНИЛС (формат 3-3-3-2) замаскирован", "PII_" in clean and (mask & 2), clean.strip())
clean, mask, _ = S.scrub("Без документа: 12345678900.")
check("11 цифр без 7/8 в начале = СНИЛС", mask & 2, clean.strip())

# --- 3. Полис ОМС (16 цифр) ---
clean, mask, _ = S.scrub("Полис ОМС 1234567890123456 выдан 2020 году.")
check("Полис ОМС (16 цифр) замаскирован", mask & 4, clean.strip())

# --- 4. E-mail ---
clean, mask, _ = S.scrub("Направить ответ на ivanov.petrov@mail.ru сегодня.")
check("E-mail замаскирован", mask & 8, clean.strip())

# --- 5. ФИО ---
clean, mask, _ = S.scrub("Пациент Иванов И.И. явился вовремя.")
check("ФИО с инициалами замаскировано", mask & 16, clean.strip())
clean, mask, _ = S.scrub("Осмотра проводил Петрова Мария Сергеевна.")
check("ФИО полностью замаскировано", mask & 16, clean.strip())

# --- 6. Медицинские числа НЕ трогаем (критично для клинической разметки!) ---
med_text = "Креатинин 86 мкмоль/л, СКФ 74 мл/мин, ЧСС 78, АД 130/80, МНО 1.05, Hb 142 г/л, дата 15.02.2017."
clean, mask, _ = S.scrub(med_text)
check("Медицинские числа сохранены байт-в-байт", clean == med_text, f"mask={mask}")
check("На медицинском тексте 0 срабатываний", mask == 0, f"mask={mask}")

# --- 7. Детерминированность и перец ---
c1, _, _ = S.scrub("Иванов И.И.")
c2, _, _ = S.scrub("Иванов И.И.")
check("Одинаковый ввод+перец = одинаковый псевдоним", c1 == c2 and "PII_" in c1)
S2 = PiiScrubber(pepper="other_pepper")
c3, _, _ = S2.scrub("Иванов И.И.")
check("Другой перец = другой псевдоним", c3 != c1)

# --- 8. Текст без ПДн проходит без изменений ---
plain = "Жалобы на тревогу и инсомнию после COVID-19. Назначен Авиандр 20 мг 2 раза в сутки."
clean, mask, _ = S.scrub(plain)
check("Текст без ПДн не изменён", clean == plain and mask == 0)

# --- 9. Смешанный боевой текст ---
mixed = ("Осмотр невролога. Пациент: Сидоров Пётр Ильич, 42 г. Жал.: трев., инсомния. "
         "Тел. +7 (383) 210-45-67. СНИЛС 112-233-445 95. СКФ 92 мл/мин. email: sidorov@yandex.ru.")
clean, mask, kinds = S.scrub(mixed)
check("Смешанный текст: 4 категории найдены", mask & 1 and mask & 2 and mask & 8 and mask & 16, str(kinds))
check("Смешанный текст: СКФ 92 не тронут", "СКФ 92 мл/мин" in clean)

# --- 10. Прогон через реальный парсер ЭМК Михаила (интеграция pii -> EHR) ---
try:
    from ehr_nlp_benchmark import HybridEHRParser  # noqa: F401

    parser = HybridEHRParser()
    ehr = ("Осмотр невролога (ф. 025/у). Иванов Олег Кириллович, М, 36 г. Код МКБ: F43.2. "
           "Жал.: трев., инсомния после COVID-19. Тел. +7 913 555-12-34. "
           "Лаб: креат. 86 мкмоль/л, Чайлд-Пью: норма. Шкалы: ГТР-7 12б, PHQ9 7б (п9=0).")
    scrubbed, mask, _ = S.scrub(ehr)
    parsed_raw = parser.parse_ehr_record(ehr)
    parsed_scrub = parser.parse_ehr_record(scrubbed)
    same_icd = parsed_raw["icd10_code"] == parsed_scrub["icd10_code"] == "F43.2"
    same_profile = (
        parsed_raw["c_profile"]["serum_creatinine_umol_l"] == parsed_scrub["c_profile"]["serum_creatinine_umol_l"]
    )
    check("Интеграция: парсер Михаила даёт тот же МКБ-10 после scrub", same_icd)
    check("Интеграция: креатинин не искажён scrub'ом", same_profile)
except ImportError as e:  # noqa: BLE001
    print(f"[SKIP] Интеграционный тест (нет парсера): {e}")

# --- 11. Пропускная способность (Метрика №13: потоковое обезличивание) ---
import random  # noqa: E402

random.seed(2026)
words = ["жалобы", "тревога", "креатинин", "СКФ", "инсомния", "Авиандр", "назначение", "контроль", "осмотр"]
chunks = []
total = 0
while total < 2_000_000:  # ~2 МБ текста
    if random.random() < 0.02:
        c = f"Иванов И.И., тел. +7 (9{random.randint(10, 99)}) {random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}. "
    else:
        c = random.choice(words) + " "
    chunks.append(c)
    total += len(c)
big = "".join(chunks)
t0 = time.perf_counter()
clean, mask, _ = S.scrub(big)
dt = time.perf_counter() - t0
mb_s = len(big.encode()) / 1e6 / dt
print(f"[i] Пропускная способность: {mb_s:.1f} МБ/с ({len(big.encode())/1e6:.1f} МБ за {dt*1000:.0f} мс, mask={mask:#x})")
check("Пропускная способность > 10 МБ/с", mb_s > 10)

print()
if FAILURES:
    print(f"[FAIL] Провалено {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("[OK] Все тесты pii_scrubber пройдены.")
