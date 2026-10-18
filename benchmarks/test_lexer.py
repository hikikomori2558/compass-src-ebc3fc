#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_lexer.py — Ахо—Корасик против наивного поиска (день 5, блок 3; метрика 2).

Прогон: python3 benchmarks/test_lexer.py
Главная идея теста — ОРАКУЛ: результат автомата обязан совпасть с тупым наивным
поиском подстрок по всем словам словаря на всех 100 виньетках. Автомат может быть
только быстрее, но никогда — «умнее» наивного поиска: любое расхождение = баг автомата.
Плюс: recall МКБ-10 против gt_icd10 виньеток, суффикс-регресс («флуконазол» -> «азол»),
пропускная способность на мегабайтном тексте.
"""
import json
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from lexer_bridge import get_lexer  # noqa: E402
from bio_paths import VIGNETTES_PATH  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


lex = get_lexer()

# --- 1. Словарь: целостность и раскладка категорий ---
check("Словарь не пуст", len(lex.dict) > 0, f"{len(lex.dict)} слов")
cat_counts = Counter(d["category"] for d in lex.dict)
check("Раскладка категорий 17 icd10 / 24 drug / 15 scale / 29 abbr",
      (cat_counts["icd10"], cat_counts["drug"], cat_counts["scale"], cat_counts["abbr"]) == (17, 24, 15, 29),
      str(dict(cat_counts)))
check("Нет пустых слов и дублей", all(d["word"] for d in lex.dict)
      and len({d["word"] for d in lex.dict}) == len(lex.dict))

# --- 2. Суффикс-регресс: наследование вывода из fail-цепочки ---
# Смещения считаем в БАЙТАХ (кириллица двухбайтная) программно — не хардкодом.
_probe = "Пациенту назначен флуконазол."
_probe_b = _probe.lower().encode("utf-8")
_flu_start = _probe_b.find(b"\xd1\x84\xd0\xbb\xd1\x83\xd0\xba\xd0\xbe\xd0\xbd\xd0\xb0\xd0\xb7\xd0\xbe\xd0\xbb")  # 'флуконазол'
hits = lex.scan(_probe)
words_at = [(m["word"], m["start"]) for m in hits]
check("«флуконазол» найден целиком (байтовое смещение)", ("флуконазол", _flu_start) in words_at, str(hits))
check("суффикс «азол» найден в хвосте (баг наследования не вернулся)",
      ("азол", _flu_start + len("флуконазол".encode()) - len("азол".encode())) in words_at, str(hits))

# --- 3. ОРАКУЛ: автомат == наивный поиск, на всех 100 виньетках ---
vignettes = json.loads(VIGNETTES_PATH.read_text(encoding="utf-8"))
check("Корпус 100 виньеток на месте", len(vignettes) == 100)

dict_words = [d["word"].encode("utf-8") for d in lex.dict]


def naive_find_all(haystack: bytes, needle: bytes):
    """Все вхождения, включая перекрывающиеся (как умеет Ахо—Корасик)."""
    out, start = [], 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return out
        out.append((i, needle))
        start = i + 1


mismatches = []
for v in vignettes:
    raw = v["raw_text"].lower().encode("utf-8")
    auto = sorted((m["start"], m["word"].encode("utf-8")) for m in lex.scan(v["raw_text"]))
    naive = []
    for w in dict_words:
        naive.extend(naive_find_all(raw, w))
    naive.sort()
    if auto != naive:
        mismatches.append((v["case_id"], len(auto), len(naive)))
check(f"Оракул: автомат == наивный поиск на {len(vignettes)} виньетках", not mismatches,
      str(mismatches[:3]))

# --- 4. Recall МКБ-10 против ground truth виньеток ---
icd_hits = 0
for v in vignettes:
    gt = v["gt_icd10"].lower()
    found = any(m["category"] == "icd10" and m["word"] == gt for m in lex.scan(v["raw_text"]))
    icd_hits += found
check(f"Recall МКБ-10 по словарю лексера: {icd_hits}/100", icd_hits == 100)

# --- 5. Пропускная способность (~2 МБ, с ростом выходного буфера через -1) ---
random.seed(2026)
words = [d["word"] for d in lex.dict]
chunks, total = [], 0
while total < 2_000_000:
    c = random.choice(words) + " "
    chunks.append(c)
    total += len(c)
big = "".join(chunks)

t0 = time.perf_counter()
hits = lex.scan(big)
dt = time.perf_counter() - t0
big_b = big.encode("utf-8")
mb_s = len(big_b) / 1e6 / dt
print(f"[i] Скан {len(big_b)/1e6:.1f} МБ: {dt*1000:.0f} мс, {len(hits)} совпадений -> {mb_s:.0f} МБ/с")
check("Пропускная способность > 5 МБ/с (Python-мост, байтовые смещения)", mb_s > 5)

# Плотность: текст собран из слов словаря — совпадений должны быть сотни тысяч
check("Текст из слов словаря даёт плотные совпадения", len(hits) > 100_000, f"{len(hits)}")
# И главное — выборочная верификация: срез байтов текста по (start,length) обязан
# совпасть со словом словаря. 1000 случайных хитов как строгая выборка.
random.seed(7)
bad = 0
for m in random.sample(hits, 1000):
    if big_b[m["start"]:m["start"] + m["length"]] != m["word"].encode("utf-8"):
        bad += 1
check("Выборка 1000 хитов: байты текста == слово словаря", bad == 0, f"расхождений {bad}")

print()
if FAILURES:
    print(f"[FAIL] Провалено {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("[OK] Все проверки лексера пройдены.")
