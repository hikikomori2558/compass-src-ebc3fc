#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pii_scrubber.py — тесты и бенчмарк обезличивания ПДн (Метрика №13, 152-ФЗ).
Прогон: python3 benchmarks/test_pii_scrubber.py — без pytest, честные [OK]/[FAIL] в консоль.
Здесь же A/B-проверка: кэшированный путь (pii_scrubber_new) и легаси (pii_scrub_stream)
обязаны давать побайтово одинаковый результат.
"""
import random
import sys
import time


from backend.bridges.pii import PiiScrubber, legacy_scrub

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

# --- 7b. A/B: кэшированный путь == легаси-путь (побайтово) ---
ab_texts = [
    "Иванов И.И., тел. +7 (913) 123-45-67, snils 123-456-789 00, mail: a.b@x.ru.",
    "Петрова Мария Сергеевна, 8 800 200 47 03, полис 1234567890123456.",
    "Креатинин 86 мкмоль/л, СКФ 74, АД 130/80. Без ПДн.",
    "",
]
ab_ok = True
for t in ab_texts:
    cached = S.scrub(t)
    legacy = legacy_scrub(t, "test_pepper")
    if (cached[0], cached[1]) != (legacy[0], legacy[1]):
        ab_ok = False
        print(f"    расхождение на: {t!r}\n    cached={cached[0]!r}\n    legacy={legacy[0]!r}")
check("A/B: кэш и легаси дают одинаковый результат", ab_ok)

# --- 7b+. Кросс-проверка криптографии: наш C-HMAC vs hashlib (OpenSSL) ---
# Псевдоним = первые 8 hex-символов HMAC-SHA256(pepper, фрагмент). Если наша
# самописная реализация SHA-256/HMAC ошиблась хоть в одном бите — тест это поймёт,
# потому что hashlib сам прогнан по тестовым векторам RFC 4231.
import hashlib  # noqa: E402
import hmac as hmac_std  # noqa: E402

_frag = "81234567890"   # матчится как телефон — фрагмент предсказуем
_tok, _tmask, _ = S.scrub(f"тел. {_frag}")
_expected = hmac_std.new(b"test_pepper", _frag.encode("utf-8"), hashlib.sha256).hexdigest()[:8]
check("C-HMAC совпадает с hashlib/RFC 4231 побайтово", _tok == f"тел. PII_{_expected}" and (_tmask & 1), f"{_tok!r} vs PII_{_expected}")

# --- 7c. Ложные срабатывания, которых быть не должно ---
for dose in [
    "Авиандр 20 мг 2 раза в сутки за 30 минут до еды.",
    "Больница №7, отделение №3, палата 12.",
    "Курс 24 недели, доза 40 мг/сут.",
]:
    clean, mask, _ = S.scrub(dose)
    check(f"Нет ложного срабатывания: {dose[:34]}...", mask == 0 and clean == dose)

# Горячая линия (публичный номер) технически матчится как телефон — так и задумано:
# скраббер не решает, чей это номер, он заменяет всё, похожее на ПДн.
clean, mask, _ = S.scrub("Телефон доверия: 8-800-200-47-03")
check("Публичный 11-значный номер тоже заменяется (политика по умолчанию)", (mask & 1) != 0)

# --- 7d. Адский блок: контракт C-API напрямую, без удобств моста ---
import ctypes  # noqa: E402

from backend.bridges.pii import SIZE_T_MAX, _bind, _load_lib  # noqa: E402

_lib = _load_lib()
_bind(_lib)   # без этого ctypes вернёт указатель как c_int и обрежет size_t — тест соврёт
check("pii_scrubber_new(NULL) честно возвращает NULL", not _lib.pii_scrubber_new(None))

# Буфера не хватает: контракт как у snprintf — возвращается (size_t)-1, содержимое out
# после этого не определено (может быть частичный вывод). Проверяем строгое: ни один
# байт не выходит за ЗАЯВЛЕННУЮ ёмкость, хотя физически памяти подложили больше.
_tiny_in = b"tel +7 913 123 45 67 and some plain text here"
_tiny_buf = ctypes.create_string_buffer(64)          # реальная память — 64 байта
_rc = _lib.pii_scrub_stream(_tiny_in, _tiny_buf, 8, b"pepper", None)   # cap=8 — заведомо мало
check("Малый буфер -> (size_t)-1", _rc == SIZE_T_MAX)
_junk = bytes(_tiny_buf)
check("Ни байта за заявленной границей cap=8", _junk[8:] == b"\x00" * 56, repr(_junk[:24]))

# Длина псевдонима — часть контракта идемпотентности: 8 hex-символов не могут
# собрать 11 цифр телефона/СНИЛС или 16 цифр полиса, поэтому повторный scrub
# гарантированно не трогает уже вставленные псевдонимы.
_sample, _, _ = S.scrub("Иванов И.И.")
_hexpart = _sample.split("PII_")[1] if "PII_" in _sample else ""
check("Псевдоним содержит ровно 8 hex-символов", len(_hexpart) == 8 and all(c in "0123456789abcdef" for c in _hexpart), _hexpart)

# Идемпотентность на случайных текстах: scrub(scrub(x)) == scrub(x).
_idem_ok = True
random.seed(42)
for _ in range(300):
    parts = []
    for _k in range(random.randint(1, 6)):
        r = random.random()
        if r < 0.3:
            parts.append(f"Иванов И.И. {random.randint(20, 80)} лет")
        elif r < 0.6:
            parts.append(f"тел. +7 9{random.randint(100, 999)} {random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}")
        else:
            parts.append(f"креатинин {random.randint(50, 200)} мкмоль/л, СКФ {random.randint(40, 120)}")
    x = "; ".join(parts) + "."
    once, _, _ = S.scrub(x)
    twice, _, _ = S.scrub(once)
    if once != twice:
        _idem_ok = False
        print(f"    не идемпотентно: {x!r} -> {once!r} -> {twice!r}")
        break
check("Идемпотентность: повторный scrub не трогает псевдонимы (300 случайных текстов)", _idem_ok)

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
print(f"[i] Сквозная (UTF-8 кодеки + мост): {mb_s:.0f} МБ/с ({len(big.encode())/1e6:.1f} МБ за {dt*1000:.0f} мс, mask={mask:#x})")

# Чистый C-конвейер: те же данные, но без кодеков и с переиспользуемым буфером.
# Это число и есть честная скорость нативного контура для слайда Метрики №13.
# Порог — env-переопределяемый регресс-страж, а не маркетинг: на Apple Silicon чистый C
# даёт ~500+ МБ/с, на серверных x86 (Ryzen 5500U в Debian-стенде) скромнее —
# фиксированный кросс-платформенный порог врал бы одной из платформ.
import os as _os  # noqa: E402

_min_c = int(_os.getenv("PII_MIN_MB_S", "150"))
big_raw = big.encode("utf-8")
t0 = time.perf_counter()
clean_raw, mask2, _ = S.scrub_bytes(big_raw)
dt2 = time.perf_counter() - t0
mb_s_c = len(big_raw) / 1e6 / dt2
print(f"[i] Чистый C-конвейер (scrub_bytes): {mb_s_c:.0f} МБ/с ({dt2*1000:.0f} мс)")
_min_e2e = int(_os.getenv("PII_MIN_E2E_MB_S", "80"))
check(f"Сквозная пропускная способность > {_min_e2e} МБ/с (регресс-страж)", mb_s > _min_e2e)
check(f"Чистый C-конвейер > {_min_c} МБ/с (регресс-страж)", mb_s_c > _min_c)
check("scrub и scrub_bytes согласованы", clean_raw.decode("utf-8") == clean and mask == mask2)

print()
if FAILURES:
    print(f"[FAIL] Провалено {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("[OK] Все тесты pii_scrubber пройдены.")
