# backend/services/ehr_extract.py — безопасный экстрактор ЭМК (tri-state, с отрицаниями и датами).
#
# Зачем он нужен. Старый HybridEHRParser (benchmarks/ehr_nlp_benchmark.py) искал подстроки и
# подставлял значения по умолчанию, из-за чего происходило ровно то, чего нельзя в клинике:
#   «эпилепсии нет»              -> эпилепсия = ДА (ложная блокировка),
#   «ИМАО отменён 60 дней назад»  -> 7 дней до отмены (ложная блокировка),
#   пустая история                -> возраст 35, «всё безопасно» (ложный ЗЕЛЁНЫЙ),
#   п.9 PHQ-9 не заполнен         -> 0 (пропущенный суицидальный риск),
#   «креатинин 1,4 мг/дл»         -> «не указан» (единицы).
#
# Контракт этого модуля:
#   * Python None = «неизвестно». False — только если в тексте есть явное отрицание
#     ИЛИ врач подтвердил отсутствие (apply_confirmations). Отсутствие упоминания — это None.
#   * Отрицание распознаётся только по ТЕСНЫМ признакам. Перекос осознанный: нераспознанное
#     отрицание даёт ложную тревогу (врач видит цитату и снимает её), а ложно распознанное
#     отрицание даёт пропущенное противопоказание. Поэтому при сомнении — «ДА» или «неизвестно».
#   * Каждое извлечённое значение несёт evidence: цитата и смещения в тексте (метрика №5).
#   * Лабораторные значения: единицы (мкмоль/л <-> мг/дл), правдоподобие, давность (> 6 мес.
#     считается неизвестным), конфликт значений.
#   * Ничего не «додумываем»: нет возраста — возраст None; нет диагноза — диагноз None.
#
# Ограничения (честно): это правила, а не языковая модель. Они не понимают длинных
# придаточных конструкций и сложной анафоры. Всё, что не распознано, уходит в «неизвестно»
# и требует подтверждения врача — это безопасное направление ошибки.
from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "ehr_extract/1"

UMOL_PER_MGDL_CREAT = 88.4     # креатинин: мг/дл -> мкмоль/л
UMOL_PER_MGDL_BILI = 17.1      # билирубин: мг/дл -> мкмоль/л
G_L_PER_G_DL = 10.0            # альбумин: г/дл -> г/л

MAX_LAB_AGE_DAYS = 183         # анализ старше ~6 мес. = «неизвестно» (почки/печень меняются)
PREGNANCY_NOT_APPLICABLE_AGE = 55   # допущение (выводится в assumptions): женщина >= 55 лет

Ev = Dict[str, Any]


# ============================================================ нормализация и сегменты
def _sentences(low: str) -> List[Tuple[int, int]]:
    """Предложения с позициями. Делим по . ! ? ; и переводам строк, но не по «п.9» и «1.4»."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    for m in re.finditer(r"(?<=[.!?;])\s+|\n+|\s*\|\s*", low):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(low):
        spans.append((pos, len(low)))
    return spans


def _sentence_of(spans: List[Tuple[int, int]], pos: int) -> Tuple[int, int]:
    for a, b in spans:
        if a <= pos < b or (pos == b and a < b):
            return a, b
    return (0, 0)


_SEG_SPLIT = re.compile(r"\s*(?:,|—|–|:|\(|\)|\s-\s)\s*|\s+(?:но|однако|при этом|а также)\s+")


def _segments(low: str, a: int, b: int) -> List[Tuple[int, int]]:
    segs: List[Tuple[int, int]] = []
    pos = a
    text = low[a:b]
    for m in _SEG_SPLIT.finditer(text):
        s, e = a + m.start(), a + m.end()
        if s > pos:
            segs.append((pos, s))
        pos = e
    if pos < b:
        segs.append((pos, b))
    return [(x, y) for x, y in segs if low[x:y].strip()]


# ============================================================ полярность упоминания
_UNC = re.compile(
    r"не\s+исключ\w*|подозр\w*|возможн\w*|вероятн\w*|\?|не\s+уточн\w*|нет\s+данных|данных\s+нет|"
    r"неизвестн\w*|не\s+известн\w*|не\s+оценива\w*|не\s+исследова\w*|не\s+обследова\w*"
)
_NEG = re.compile(
    r"(?<!\w)нет(?!\w)|(?<!\w)не(?!\w)|(?<!\w)без(?!\w)|отрицает\w*|отрицан\w*|отрицательн\w*|"
    r"исключен\w*|исключён\w*|отсутству\w+|не\s+было|не\s+выявл\w*|не\s+отмеч\w*|не\s+страда\w*"
)
_NEG_LEAD = re.compile(
    r"^\s*(?:(?:пациент\w*|она|он)\s+)?(?:отрицает\w*|отрицан\w*|без|не\s+страда\w*|не\s+имеет|нет|не\s+было)\b"
)
_NEG_TRAIL_BARE = re.compile(
    r"^\s*(?:все|всё|это|данное)?\s*(?:отрицает\w*|нет|не\s+было|не\s+выявл\w*|исключен\w*|отсутству\w+)\s*$"
)
_FAMILY = re.compile(
    r"у\s+(?:матери|отца|брата|сестры|мамы|папы|бабушки|дедушки|родственник\w*)|в\s+семье|"
    r"семейн\w+|наследствен\w+|родител\w+"
)
_AFFIRM_VERBS = re.compile(
    r"в\s+анамнезе|страда\w+|принима\w+|имеется|отмеча\w+|диагностир\w+|установлен\w*|наблюда\w+|"
    r"состоит|получает|находится"
)
_WORD = re.compile(r"\w+", re.U)


def _is_bare(seg: str) -> bool:
    words = _WORD.findall(seg)
    return len(words) <= 4 and not re.search(r"\d", seg) and not _AFFIRM_VERBS.search(seg)


def _window_words(seg_text: str, ms: int, me: int, n: int = 3) -> Tuple[str, str]:
    before = seg_text[:ms]
    after = seg_text[me:]
    wb = " ".join(_WORD.findall(before)[-n:])
    wa = " ".join(_WORD.findall(after)[:n])
    return wb, wa


def _polarity(low: str, sent: Tuple[int, int], ms: int, me: int) -> str:
    """POS / NEG / UNC / FAM для упоминания [ms:me] внутри предложения sent."""
    segs = _segments(low, *sent)
    # какой сегмент содержит упоминание
    idx = next((i for i, (a, b) in enumerate(segs) if a <= ms < b), None)
    if idx is None:
        return "POS"
    a, b = segs[idx]
    seg_text = low[a:b]

    if _FAMILY.search(seg_text):
        return "FAM"
    if _UNC.search(seg_text):
        return "UNC"

    # 1. тесное окно: до 3 слов вокруг упоминания внутри сегмента
    wb, wa = _window_words(seg_text, ms - a, me - a)
    if _NEG.search(wb) or _NEG.search(wa):
        return "NEG"

    # 2. список после ведущего отрицания: «отрицает эпилепсию, ЧМТ, беременность»
    active = False
    for i, (x, y) in enumerate(segs[: idx + 1]):
        t = low[x:y]
        if _NEG_LEAD.search(t):
            active = True
        elif active and not _is_bare(t):
            active = False
        if i == idx and active and (_is_bare(t) or _NEG_LEAD.search(t)):
            return "NEG"

    # 3. список перед «голым» замыкающим отрицанием: «эпилепсия, ЧМТ — отрицает»
    if len(segs) >= 2:
        last_a, last_b = segs[-1]
        last = low[last_a:last_b].strip(" .!?;")
        if _NEG_TRAIL_BARE.search(last) and idx < len(segs) - 1 and _is_bare(seg_text):
            return "NEG"
    return "POS"


def _snippet(text: str, a: int, b: int, pad: int = 28) -> str:
    return text[max(0, a - pad): min(len(text), b + pad)].replace("\n", " ").strip()


def _ev(text: str, a: int, b: int, value: Any, source: str = "text", note: str = "") -> Ev:
    e: Ev = {"value": value, "text": _snippet(text, a, b), "start": a, "end": b, "source": source}
    if note:
        e["note"] = note
    return e


def _resolve_concept(text: str, low: str, spans, rx: "re.Pattern[str]",
                     warnings: List[str], label: str) -> Tuple[Optional[bool], List[Ev]]:
    """Сводит все упоминания понятия к tri-state. Конфликт ДА/НЕТ -> ДА (консервативно)."""
    pos, neg, unc = [], [], []
    for m in rx.finditer(low):
        sent = _sentence_of(spans, m.start())
        pol = _polarity(low, sent, m.start(), m.end())
        if pol == "FAM":
            continue
        {"POS": pos, "NEG": neg, "UNC": unc}[pol].append(_ev(text, m.start(), m.end(), pol))
    if pos:
        if neg:
            warnings.append(f"{label}: в тексте одновременно есть упоминание и отрицание — принято «да», проверьте.")
        return True, pos
    if unc:
        warnings.append(f"{label}: формулировка неоднозначна («не исключено», «подозрение», «нет данных») — "
                        f"считается неизвестным, уточните.")
        return None, unc
    if neg:
        return False, neg
    return None, []


# ============================================================ словари понятий
# Списки препаратов/состояний — рабочие; ПРОВЕРИТЬ по ОХЛП перед защитой (docs/VERIFICATION.md).
RX_EPILEPSY = re.compile(
    r"эпилепс\w*|судорожн\w*\s+припад\w*|органическ\w+\s+(?:поражени\w+|заболеван\w+)\s+"
    r"(?:головного\s+мозга|цнс|гм)|(?<![a-zа-я])[gг]4[01](?:\.\d)?(?![\d])|(?<![a-zа-я])f0\d(?:\.\d)?(?![\d])"
)
RX_LACTASE = re.compile(r"лактаз\w*|лактоз\w*|галактоз\w*|мальабсорбц\w*|lct-?13910")
RX_PREGNANCY = re.compile(r"беременн\w*|лактаци\w*|грудн\w+\s+вскармлива\w*|кормит\s+грудью|(?<!\w)гв(?!\w)")
RX_MAOI = re.compile(
    r"(?<!\w)имао(?!\w)|ингибитор\w*\s+(?:моно\s*амино\s*оксидаз\w*|мао)|фенелзин\w*|транилципромин\w*|"
    r"моклобемид\w*|ниаламид\w*|пиразидол\w*|пирлиндол\w*|селегилин\w*|разагилин\w*"
)
RX_HEPATIC = re.compile(r"цирроз\w*|печ[её]ночн\w*\s+недостаточност\w*|недостаточност\w*\s+печени")
RX_PSYCHOSIS = re.compile(r"(?<!\w)бред\w*|галлюцинац\w*|психотическ\w*|оклик\w*|преследован\w*")
RX_SUICIDAL = re.compile(
    r"суицид\w*|самоубийств\w*|самоповрежден\w*|не\s+хочет\s+жить|мысл\w*\s+о\s+смерти"
)
RX_DRIVES = re.compile(r"вод(?:ит|ил)\w*\s*(?:авто\w*|машин\w*)|за\s+рул[её]м|(?<!\w)водител\w*")
RX_SSRI = re.compile(
    r"сертралин\w*|эсциталопрам\w*|циталопрам\w*|пароксетин\w*|флуоксетин\w*|флувоксамин\w*|"
    r"венлафаксин\w*|дулоксетин\w*|сиозс|сиозсин"
)
RX_SSRI_FAIL = re.compile(r"непереносим\w*|неэффективн\w*|побочн\w*|бросил\w*|отмен\w*\s+из-за|не\s+подош\w*")

_SEROTONERGIC = ("сертралин", "эсциталопрам", "циталопрам", "пароксетин", "флуоксетин", "флувоксамин",
                 "амитриптилин", "кломипрамин", "имипрамин", "трамадол", "суматриптан", "золмитриптан",
                 "литий", "зверобой")
_STRONG_2D6 = ("дифенгидрамин", "кетоконазол", "хинидин", "гидроксихлорохин", "тербинафин",
               "пароксетин", "флуоксетин")
_SEDATIVES = ("бензодиазеп", "диазепам", "алпразолам", "клоназепам", "лоразепам", "феназепам",
              "нейролептик", "снотворн", "алкогол", "этанол")
COMED_METOPROLOL, COMED_DULOXETINE, COMED_CIPROFLOXACIN, COMED_FLUCONAZOLE = 1, 2, 4, 8
COMED_STRONG_CYP2D6_INHIB, COMED_SEROTONERGIC, COMED_SEDATIVES_ETHANOL = 16, 32, 64


def _comedication(text: str, low: str, spans, ev: Dict[str, List[Ev]]) -> int:
    """Маска только из ПОЛОЖИТЕЛЬНЫХ упоминаний («алкоголь не употребляет» маску не даёт)."""
    mask = 0
    groups = [
        (_SEROTONERGIC, COMED_SEROTONERGIC), (_STRONG_2D6, COMED_STRONG_CYP2D6_INHIB),
        (("метопролол",), COMED_METOPROLOL), (("дулоксетин",), COMED_DULOXETINE),
        (("ципрофлоксацин",), COMED_CIPROFLOXACIN), (("флуконазол",), COMED_FLUCONAZOLE),
        (_SEDATIVES, COMED_SEDATIVES_ETHANOL),
    ]
    for words, bit in groups:
        for w in words:
            for m in re.finditer(re.escape(w), low):
                sent = _sentence_of(spans, m.start())
                if _polarity(low, sent, m.start(), m.end()) == "POS":
                    mask |= bit
                    ev.setdefault("comedication_bitmask", []).append(_ev(text, m.start(), m.end(), w))
                    break
    return mask


# ============================================================ даты и давность
_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
           "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def _last_day(y: int, m: int) -> date:
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return nxt - timedelta(days=1)


def _dates_in(s: str, skip: Tuple[int, int]) -> List[date]:
    """Даты внутри фрагмента s. skip — диапазон значения анализа, его не трогаем."""
    out: List[date] = []
    mask = list(s)
    for i in range(max(0, skip[0]), min(len(s), skip[1])):
        mask[i] = " "
    t = "".join(mask)

    def add(d: Optional[date]) -> None:
        if d:
            out.append(d)

    for m in re.finditer(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)", t):
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yy = yy + 2000 if yy < 100 else yy
        try:
            add(date(yy, mm, dd))
        except ValueError:
            pass
    t2 = re.sub(r"(?<!\d)\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})(?!\d)", " ", t)
    for m in re.finditer(r"(?<![\d.])(\d{1,2})\.(\d{4})(?!\d)", t2):
        mm, yy = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1990 <= yy <= 2100:
            add(_last_day(yy, mm))
    for m in re.finditer(r"([а-я]{3,8})\s+(\d{4})(?!\d)", t2):
        mon = _MONTHS.get(m.group(1)[:3])
        yy = int(m.group(2))
        if mon and 1990 <= yy <= 2100:
            add(_last_day(yy, mon))
    t3 = re.sub(r"(?<![\d.])\d{1,2}\.\d{4}(?!\d)", " ", t2)
    for m in re.finditer(r"(?<![\d.,])(19\d{2}|20\d{2})(?![\d.,])", t3):
        add(date(int(m.group(1)), 1, 1))   # только год: берём 1 января (консервативно: «старше»)
    return out


# ============================================================ лабораторные значения

_LAB_NAMES = r"креат|билир|альбум|скф|egfr|(?<!\w)мно(?!\w)|inr|(?<!\w)алт(?!\w)|(?<!\w)аст(?!\w)"


def _local_dates(low: str, sa: int, sb: int, name_start: int, val_end: int) -> List[date]:
    """Дата именно этого значения: сразу после него (до следующего значения) либо перед названием анализа."""
    after = low[val_end:min(sb, val_end + 40)]
    cut = re.search(rf",\s*(?=\d|{_LAB_NAMES})|;|\.\s+[а-яa-z]", after)
    if cut:
        after = after[:cut.start()]
    dts = _dates_in(after, (0, 0))
    if dts:
        return dts
    before = low[max(sa, name_start - 25):name_start]
    if not re.search(_LAB_NAMES, before):
        return _dates_in(before, (0, 0))
    return []

_NUM = r"(\d+(?:[.,]\d+)?)"

LAB_CFG: Dict[str, Dict[str, Any]] = {
    "serum_creatinine_umol_l": {
        "name": r"креатинин\w*|креат(?!\w)\.?|creatinine|(?<![a-z])cr(?![a-z])",
        "unit": r"мкмоль\s*[/\\]?\s*л|µмоль\s*/\s*л|[µu]mol\s*/\s*l|мг\s*/\s*дл|мг\s*%|mg\s*/\s*dl",
        "to_std": {"umol": 1.0, "mgdl": UMOL_PER_MGDL_CREAT},
        "range": (15.0, 3000.0), "label": "креатинин",
        "no_unit": lambda v: ("umol" if v >= 30 else None),
    },
    "bilirubin_umol_l": {
        "name": r"билирубин\w*",
        "unit": r"мкмоль\s*[/\\]?\s*л|µмоль\s*/\s*л|[µu]mol\s*/\s*l|мг\s*/\s*дл|мг\s*%|mg\s*/\s*dl",
        "to_std": {"umol": 1.0, "mgdl": UMOL_PER_MGDL_BILI},
        "range": (1.0, 1000.0), "label": "билирубин",
        "no_unit": lambda v: None,
    },
    "albumin_g_l": {
        "name": r"альбумин\w*",
        "unit": r"г\s*[/\\]\s*л|г\s*[/\\]\s*дл|g\s*/\s*l|g\s*/\s*dl",
        "to_std": {"gl": 1.0, "gdl": G_L_PER_G_DL},
        "range": (5.0, 60.0), "label": "альбумин",
        "no_unit": lambda v: ("gl" if v >= 15 else ("gdl" if v <= 7 else None)),
    },
    "inr": {
        "name": r"(?<!\w)мно(?!\w)|(?<![a-z])inr(?![a-z])",
        "unit": r"(?!x)x",
        "to_std": {"none": 1.0}, "range": (0.5, 10.0), "label": "МНО",
        "no_unit": lambda v: "none",
    },
    "alt_u_l": {
        "name": r"(?<!\w)алт(?!\w)|(?<![a-z])alt(?![a-z])",
        "unit": r"ед\s*/\s*л|е\s*д\s*/\s*л|u\s*/\s*l|ме\s*/\s*л",
        "to_std": {"none": 1.0}, "range": (1.0, 5000.0), "label": "АЛТ",
        "no_unit": lambda v: "none",
    },
    "ast_u_l": {
        "name": r"(?<!\w)аст(?!\w)|(?<![a-z])ast(?![a-z])",
        "unit": r"ед\s*/\s*л|е\s*д\s*/\s*л|u\s*/\s*l|ме\s*/\s*л",
        "to_std": {"none": 1.0}, "range": (1.0, 5000.0), "label": "АСТ",
        "no_unit": lambda v: "none",
    },
}


def _unit_kind(unit: str) -> Optional[str]:
    u = unit.replace(" ", "").lower()
    if not u:
        return None
    if "мг" in u or "mg" in u:
        return "mgdl"
    if "мкмоль" in u or "µмоль" in u or "umol" in u or "µmol" in u:
        return "umol"
    if "г/дл" in u or "г\\дл" in u or "g/dl" in u:
        return "gdl"
    if "г/л" in u or "г\\л" in u or "g/l" in u:
        return "gl"
    return "none"


def _extract_lab(text: str, low: str, spans, field: str, as_of: date,
                 warnings: List[str]) -> Tuple[Optional[float], List[Ev]]:
    cfg = LAB_CFG[field]
    rx = re.compile(rf"(?:{cfg['name']})[^\d;\n<>≤≥]{{0,28}}?{_NUM}\s*({cfg['unit']})?", re.I)
    cands: List[Tuple[float, Optional[date], Ev]] = []
    problems: List[str] = []
    for m in rx.finditer(low):
        raw = float(m.group(1).replace(",", "."))
        unit = _unit_kind(m.group(2) or "")
        kind = unit
        if kind is None or kind == "none" and "none" not in cfg["to_std"]:
            kind = cfg["no_unit"](raw)
            if kind is None:
                problems.append(f"{cfg['label']} {m.group(1)} без единиц измерения — не использовано")
                continue
        if kind not in cfg["to_std"]:
            problems.append(f"{cfg['label']}: единицы «{m.group(2)}» не подходят — не использовано")
            continue
        val = raw * cfg["to_std"][kind]
        lo, hi = cfg["range"]
        if not (lo <= val <= hi):
            problems.append(f"{cfg['label']} = {raw:g} вне правдоподобного диапазона — не использовано")
            continue
        sa, sb = _sentence_of(spans, m.start())
        dts = _local_dates(low, sa, sb, m.start(), m.end())
        dts = [d for d in dts if d <= as_of + timedelta(days=1)]
        d = max(dts) if dts else None
        note = f"{raw:g} {m.group(2) or '(ед. по умолчанию)'}" if kind != "umol" or unit != "umol" else ""
        cands.append((val, d, _ev(text, m.start(), m.end(), round(val, 2), note=note)))
    for p in problems:
        warnings.append(p)
    if not cands:
        return None, []
    fresh = []
    for val, d, ev in cands:
        if d is not None and (as_of - d).days > MAX_LAB_AGE_DAYS:
            warnings.append(f"{cfg['label']} от {d.isoformat()} устарел (> {MAX_LAB_AGE_DAYS // 30} мес.) — "
                            f"считается неизвестным, нужен свежий анализ.")
            continue
        fresh.append((val, d, ev))
    if not fresh:
        return None, [c[2] for c in cands]
    dated = [c for c in fresh if c[1] is not None]
    if dated:
        best = max(dated, key=lambda c: c[1])
        return best[0], [best[2]]
    vals = sorted(c[0] for c in fresh)
    if len(vals) > 1 and (vals[-1] - vals[0]) / max(vals[-1], 1e-9) > 0.10:
        warnings.append(f"{cfg['label']}: несколько разных значений без дат — не использовано, уточните.")
        return None, [c[2] for c in fresh]
    return fresh[0][0], [fresh[0][2]]


def _extract_egfr_reported(text: str, low: str, spans, as_of: date,
                           warnings: List[str]) -> Tuple[Optional[float], List[Ev]]:
    rx = re.compile(rf"(скф|egfr|ckd-?epi|клиренс\w*\s+креатинин\w*|(?<!\w)кк(?!\w))[^\d;\n]{{0,25}}?"
                    rf"([<>≤≥]\s*)?{_NUM}", re.I)
    best: Optional[Tuple[float, Optional[date], Ev]] = None
    for m in rx.finditer(low):
        if m.group(2):
            warnings.append("СКФ указана неравенством — точное значение неизвестно, не использовано.")
            continue
        v = float(m.group(3).replace(",", "."))
        if not (1.0 <= v <= 200.0):
            warnings.append(f"СКФ = {v:g} вне правдоподобного диапазона — не использовано.")
            continue
        sa, sb = _sentence_of(spans, m.start())
        dts = [d for d in _local_dates(low, sa, sb, m.start(), m.end()) if d <= as_of + timedelta(days=1)]
        d = max(dts) if dts else None
        if d is not None and (as_of - d).days > MAX_LAB_AGE_DAYS:
            warnings.append(f"СКФ от {d.isoformat()} устарела — считается неизвестной.")
            continue
        cand = (v, d, _ev(text, m.start(), m.end(), v))
        if best is None or (d is not None and (best[1] is None or d > best[1])) or (d is None and best[1] is None and v < best[0]):
            best = cand
    if best is None:
        return None, []
    return best[0], [best[2]]


# ============================================================ шкалы
def _scale(low: str, rx: str, lo: int, hi: int, warnings: List[str], label: str,
           take: str = "first") -> Optional[int]:
    vals: List[int] = []
    for m in re.finditer(rx, low):
        v = int(m.group(1))
        if lo <= v <= hi:
            vals.append(v)
        else:
            warnings.append(f"{label} = {v} вне допустимого диапазона {lo}–{hi} — не использовано.")
    if not vals:
        return None
    if len(set(vals)) > 1:
        if take == "max":
            warnings.append(f"{label}: несколько значений {sorted(set(vals))} — принято наибольшее (консервативно).")
            return max(vals)
        warnings.append(f"{label}: противоречивые значения {sorted(set(vals))} — не использовано.")
        return None
    return vals[0]


# ============================================================ плоский JSON (FHIR-подобный)
def _flatten_json(doc: Any) -> str:
    """Приводит JSON-шаблон к тексту, чтобы работал один и тот же безопасный путь разбора."""
    lines: List[str] = []
    if not isinstance(doc, dict):
        return json.dumps(doc, ensure_ascii=False)
    subj = doc.get("subject") if isinstance(doc.get("subject"), dict) else {}
    if subj.get("age") is not None:
        lines.append(f"Возраст: {subj['age']} лет.")
    g = subj.get("gender")
    if g in ("male", "female"):
        lines.append("Пол: мужчина." if g == "male" else "Пол: женщина.")
    if doc.get("diagnosis_icd10"):
        lines.append(f"МКБ-10: {doc['diagnosis_icd10']}.")
    if doc.get("clinical_notes"):
        lines.append(str(doc["clinical_notes"]).rstrip(".") + ".")
    labs = doc.get("labs") if isinstance(doc.get("labs"), dict) else {}
    names = {"creatinine_umol_l": "Креатинин: {} мкмоль/л.", "creatinine_mgdl": "Креатинин: {} мг/дл.",
             "bilirubin_umol_l": "Билирубин: {} мкмоль/л.", "albumin_g_l": "Альбумин: {} г/л.",
             "inr": "МНО: {}.", "alt_u_l": "АЛТ: {} Ед/л.", "ast_u_l": "АСТ: {} Ед/л.",
             "egfr": "СКФ: {} мл/мин."}
    for k, v in labs.items():
        if v is None:
            continue
        lines.append(names[k].format(v) if k in names else f"{v}.".replace("..", "."))
    sc = doc.get("scales") if isinstance(doc.get("scales"), dict) else {}
    parts = []
    if "PHQ9" in sc:
        parts.append(f"PHQ-9 = {sc['PHQ9']}")
    if "PHQ9_item9" in sc:
        parts.append(f"(п.9 = {sc['PHQ9_item9']})")
    if "GAD7" in sc:
        parts.append(f"GAD-7 = {sc['GAD7']}")
    if "CSSRS" in sc:
        parts.append(f"C-SSRS = {sc['CSSRS']}")
    if "MDQ" in sc:
        parts.append(f"MDQ = {sc['MDQ']}")
    if parts:
        lines.append("Шкалы: " + ", ".join(parts) + ".")
    return "\n".join(lines)


# ============================================================ основной разбор
_CP_RANK = {"N": 0, "A": 1, "B": 2, "C": 3}
_CYR2LAT = {"а": "A", "a": "A", "в": "B", "b": "B", "с": "C", "c": "C"}


def extract(raw: str, as_of: Optional[date] = None) -> Dict[str, Any]:
    """Главная точка входа. None во всех полях = «неизвестно»."""
    as_of = as_of or date.today()
    warnings: List[str] = []
    assumptions: List[str] = []
    evidence: Dict[str, List[Ev]] = {}
    fmt = "text"

    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            text = _flatten_json(json.loads(stripped))
            fmt = "json"
        except (ValueError, TypeError):
            text = raw
            warnings.append("JSON не разобран — обработан как обычный текст.")
    else:
        text = raw
    low = text.lower()
    spans = _sentences(low)

    # ------------------------------------------------------------ диагноз
    icds = [m.group(1).upper() for m in re.finditer(r"(?<![a-zа-я0-9])([fF]\d{2}(?:\.\d{1,2})?)(?![\d])", text)]
    icd_primary = next((c for c in icds if re.match(r"F4[0-8]", c)), icds[0] if icds else None)
    # F0x (органические расстройства) — это и маркер противопоказания, а не диагноз-показание
    if icd_primary and icd_primary.startswith("F0") and not any(re.match(r"F4[0-8]", c) for c in icds):
        icd_primary = None

    # ------------------------------------------------------------ возраст и пол
    ages: List[Tuple[int, int, int]] = []
    for rx in (r"(?<![\d.])(\d{1,3})\s*(?:лет|года|год)(?!\w)", r"(?<![\d.])(\d{1,3})\s*г\.(?!\s*р)",
               r"возраст\w*\s*[:=]?\s*(\d{1,3})(?!\d)"):
        for m in re.finditer(rx, low):
            v = int(m.group(1))
            if 0 < v < 121:
                ages.append((v, m.start(1), m.end(1)))
    age: Optional[int] = None
    if ages:
        uniq = sorted({a[0] for a in ages})
        if len(uniq) == 1:
            age = uniq[0]
            evidence["age"] = [_ev(text, ages[0][1], ages[0][2], age)]
        else:
            warnings.append(f"Возраст: противоречивые значения {uniq} — не использовано.")

    fem = re.search(r"(?<!\w)(?:женщина|жен\.|женск\w*|пациентка)(?!\w)|(?<![\w])ж(?=\s*[,)/]|\s+\d{1,3}\s*(?:лет|л|г))", low)
    mal = re.search(r"(?<!\w)(?:мужчина|муж\.|мужск\w*)(?!\w)|(?<![\w])м(?=\s*[,)/]|\s+\d{1,3}\s*(?:лет|л|г))", low)
    sex: Optional[int] = None
    if fem and not mal:
        sex = 0
        evidence["sex"] = [_ev(text, fem.start(), fem.end(), "ж")]
    elif mal and not fem:
        sex = 1
        evidence["sex"] = [_ev(text, mal.start(), mal.end(), "м")]
    elif fem and mal:
        warnings.append("Пол: в тексте есть и мужские, и женские маркеры — не использовано.")

    # ------------------------------------------------------------ состояния
    epilepsy, ev = _resolve_concept(text, low, spans, RX_EPILEPSY, warnings, "Эпилепсия/органическое поражение ЦНС")
    if ev:
        evidence["has_epilepsy_or_organic_cns"] = ev
    lactase, ev = _resolve_concept(text, low, spans, RX_LACTASE, warnings, "Дефицит лактазы/непереносимость галактозы")
    if ev:
        evidence["has_lactase_deficiency"] = ev
    preg, ev = _resolve_concept(text, low, spans, RX_PREGNANCY, warnings, "Беременность/лактация")
    if ev:
        evidence["is_pregnant_or_lactating"] = ev
    if preg is None:
        if sex == 1:
            preg = False
            evidence["is_pregnant_or_lactating"] = [{"value": False, "text": "пол мужской", "source": "derived"}]
        elif sex == 0 and age is not None and age >= PREGNANCY_NOT_APPLICABLE_AGE:
            preg = False
            evidence["is_pregnant_or_lactating"] = [{"value": False, "text": f"женщина, {age} лет", "source": "derived"}]
            assumptions.append(f"Беременность/лактация не рассматривалась: женщина {age} лет "
                               f"(допущение для возраста ≥ {PREGNANCY_NOT_APPLICABLE_AGE}, подтвердите при сомнении).")

    hepatic_doc, ev = _resolve_concept(text, low, spans, RX_HEPATIC, warnings, "Цирроз/печёночная недостаточность")
    if ev:
        evidence["hepatic_insufficiency_documented"] = ev

    # ------------------------------------------------------------ ИМАО и срок после отмены
    maoi_ever, mev = _resolve_concept(text, low, spans, RX_MAOI, warnings, "ИМАО")
    days_since_maoi: Optional[int] = None
    if maoi_ever is True:
        evidence["maoi_ever"] = mev
        days_since_maoi, note = _maoi_timing(low, spans, [e["start"] for e in mev])
        if note:
            assumptions.append(note)
        if days_since_maoi is None:
            warnings.append("ИМАО в анамнезе, но срок после отмены не указан — уточните (противопоказание при ≤ 14 сут.).")
        else:
            evidence["days_since_maoi"] = [{"value": days_since_maoi, "text": note or "срок указан в тексте", "source": "text"}]
    elif mev:
        evidence["maoi_ever"] = mev

    # ------------------------------------------------------------ лаборатория
    labs: Dict[str, Optional[float]] = {}
    for field in LAB_CFG:
        val, ev = _extract_lab(text, low, spans, field, as_of, warnings)
        labs[field] = val
        if ev:
            evidence[field] = ev
    egfr_rep, ev = _extract_egfr_reported(text, low, spans, as_of, warnings)
    if ev:
        evidence["egfr_reported"] = ev

    # класс Чайлд-Пью, названный текстом
    cp_text: Optional[str] = None
    m = re.search(r"чайлд[\s\-–]*пью[^.;\n]{0,25}?(?:класс\w*\s*)?(?<![a-zа-я])([abcавс]|норм\w*)(?![a-zа-я])", low)
    if m:
        tok = m.group(1)
        cp_text = "N" if tok.startswith("норм") else _CYR2LAT.get(tok)
        if cp_text:
            evidence["child_pugh_class_text"] = [_ev(text, m.start(), m.end(), cp_text)]
    if cp_text is None:
        m = re.search(r"цирроз\w*[^.;\n]{0,30}?класс\w*\s*(?<![a-zа-я])([abcавс])(?![a-zа-я])", low)
        if m:
            cp_text = _CYR2LAT.get(m.group(1))
            if cp_text:
                evidence["child_pugh_class_text"] = [_ev(text, m.start(), m.end(), cp_text)]
    if cp_text is None:
        m = re.search(r"(?:печ[её]ночные\s+пробы|функци\w+\s+печени)[^.;\n]{0,20}?в\s+норме", low)
        if m:
            cp_text = "N"
            evidence["child_pugh_class_text"] = [_ev(text, m.start(), m.end(), "N")]

    # ------------------------------------------------------------ ЭКГ и фармакогенетика
    qtc = None
    m = re.search(r"qtc?[^\d\n;]{0,8}(\d{3})(?!\d)", low)
    if m and 300 <= int(m.group(1)) <= 700:
        qtc = float(m.group(1))
        evidence["ecg_qtc_ms"] = [_ev(text, m.start(), m.end(), qtc)]
    cyp: Optional[int] = None
    if re.search(r"cyp2d6[^.;\n]{0,12}\*?4\s*/\s*\*?4|медленн\w+\s+метаболизатор\w*", low):
        cyp = 2
    elif re.search(r"промежуточн\w+\s+метаболизатор\w*|cyp2d6[^.;\n]{0,12}\*?4\s*/\s*\*?1", low):
        cyp = 1
    elif re.search(r"ультрабыстр\w+\s+метаболизатор\w*|ультра-?быстр\w*", low):
        cyp = 3
    elif re.search(r"cyp2d6\s*(?:[:=]\s*)?(?:норма\w*|nm)|нормальн\w+\s+метаболизатор\w*", low):
        cyp = 0

    comed = _comedication(text, low, spans, evidence)

    drives, ev = _resolve_concept(text, low, spans, RX_DRIVES, warnings, "Вождение")
    if ev:
        evidence["drives_vehicle"] = ev

    # ------------------------------------------------------------ психометрия и риск
    # Числительные словами («одиннадцать баллов») — реальный формат выписок;
    # hold-out HO-18 ловил именно это. Нормализуем до цифр до всех regex шкал.
    _WORDS = {"ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
              "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
              "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
              "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
              "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40}
    _TENS = {"двадцать": 2, "тридцать": 3, "сорок": 4}
    _UNITS_AFTER_TENS = {"один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
                         "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
    import re as _re
    _ALT = "|".join(sorted(_WORDS, key=len, reverse=True))   # длинные раньше: «одиннадцать» до «один»
    def _words_to_digits(t: str) -> str:
        t = _re.sub(r"\b(" + _ALT + r")(?![а-яё])", lambda m: str(_WORDS[m.group(1)]), t)
        # составные: «двадцать два» → 22 (после подстановки десятков: «20 2»)
        return _re.sub(r"\b(20|30|40)\s+(\d)(?!\d)", lambda m: str(int(m.group(1)) + int(m.group(2))), t)
    low = _words_to_digits(low)
    phq9 = _scale(low, r"phq[-\s]?9\s*[:=\-–]?\s*(\d{1,2})(?!\d)", 0, 27, warnings, "PHQ-9")
    item9 = _scale(low, r"(?:п\.?\s?9|пункт\s*9|вопрос\s*(?:№\s*)?9|item\s*9|q9)\s*[:=]?\s*(\d)(?!\d)",
                   0, 3, warnings, "PHQ-9 п.9", take="max")

    gad7 = _scale(low, r"(?:gad[-\s]?7|гтр[-\s]?7)\s*[:=\-–]?\s*(\d{1,2})(?!\d)", 0, 21, warnings, "GAD-7")
    mdq = _scale(low, r"mdq\s*[:=\-–]?\s*(\d{1,2})(?!\d)", 0, 13, warnings, "MDQ")
    cssrs = _scale(low, r"c[-\s]?ssrs\s*[:=\-–]?\s*(\d)(?!\d)", 0, 5, warnings, "C-SSRS", take="max")
    if cssrs is None and re.search(r"c[-\s]?ssrs[^.;\n]{0,15}(?:отриц\w*|нет)", low):
        cssrs = 0
    if phq9 is not None and item9 is not None and item9 > 0 and phq9 < item9:
        warnings.append(f"PHQ-9: сумма {phq9} меньше балла п.9 = {item9} — противоречие, проверьте.")
    for name, val in (("phq9_total", phq9), ("phq9_item9_suicidal", item9), ("gad7_total", gad7),
                      ("mdq_bipolar_score", mdq), ("cssrs_score", cssrs)):
        if val is not None:
            rxs = {"phq9_total": r"phq[-\s]?9", "phq9_item9_suicidal": r"п\.?\s?9|пункт\s*9|вопрос\s*(?:№\s*)?9|item\s*9|q9",
                   "gad7_total": r"gad[-\s]?7|гтр[-\s]?7", "mdq_bipolar_score": r"mdq", "cssrs_score": r"c[-\s]?ssrs"}[name]
            mm = re.search(rxs, low)
            if mm:
                evidence[name] = [_ev(text, mm.start(), min(len(text), mm.end() + 8), val)]

    suicidal_doc, ev = _resolve_concept(text, low, spans, RX_SUICIDAL, warnings, "Суицидальные мысли")
    if ev:
        evidence["suicidal_ideation_documented"] = ev
    psychosis, ev = _resolve_concept(text, low, spans, RX_PSYCHOSIS, warnings, "Психотическая симптоматика")
    if ev:
        evidence["has_psychotic_symptoms"] = ev

    failed_ssri: Optional[bool] = None
    for m in RX_SSRI.finditer(low):
        sa, sb = _sentence_of(spans, m.start())
        sent = low[sa:sb]
        if RX_SSRI_FAIL.search(sent) and _polarity(low, (sa, sb), m.start(), m.end()) == "POS":
            failed_ssri = True
            evidence["failed_first_line_ssri"] = [_ev(text, m.start(), m.end(), True)]
            break

    profile: Dict[str, Any] = {
        "age": age, "sex": sex,
        "is_pregnant_or_lactating": preg,
        "has_epilepsy_or_organic_cns": epilepsy,
        "has_lactase_deficiency": lactase,
        "maoi_ever": maoi_ever, "days_since_maoi": days_since_maoi,
        "hepatic_insufficiency_documented": hepatic_doc,
        "child_pugh_class_text": cp_text,
        "egfr_reported": egfr_rep,
        "serum_creatinine_umol_l": labs["serum_creatinine_umol_l"],
        "bilirubin_umol_l": labs["bilirubin_umol_l"], "albumin_g_l": labs["albumin_g_l"],
        "inr": labs["inr"], "alt_u_l": labs["alt_u_l"], "ast_u_l": labs["ast_u_l"],
        "comedication_bitmask": comed, "drives_vehicle": drives,
        "cyp2d6_phenotype": cyp, "ecg_qtc_ms": qtc,
    }
    clinical_state: Dict[str, Any] = {
        "phq9_total": phq9, "phq9_item9_suicidal": item9, "gad7_total": gad7,
        "cssrs_score": cssrs, "mdq_bipolar_score": mdq,
        "has_psychotic_symptoms": psychosis, "suicidal_ideation_documented": suicidal_doc,
        "failed_first_line_ssri": failed_ssri,
    }
    return {
        "schema": SCHEMA, "format": fmt, "as_of": as_of.isoformat(),
        "icd10_code": icd_primary, "icd10_all": icds,
        "profile": profile, "clinical_state": clinical_state,
        "evidence": evidence, "assumptions": assumptions, "warnings": warnings,
        "text_for_evidence": text,
    }


# ============================================================ срок после отмены ИМАО
_NUMWORDS = {"один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
             "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
_UNITS = (("дн", 1), ("сут", 1), ("недел", 7), ("нед", 7), ("месяц", 30), ("мес", 30), ("год", 365), ("лет", 365))


def _unit_days(word: str) -> Optional[int]:
    for stem, d in _UNITS:
        if word.startswith(stem):
            return d
    return None


def _maoi_timing(low: str, spans, starts: List[int]) -> Tuple[Optional[int], str]:
    """(дней с момента отмены | 0 если принимает | None если срок не назван, пояснение)."""
    for st in starts:
        a, b = _sentence_of(spans, st)
        s = low[a:b]
        stopped = re.search(r"отмен\w+|прекращ\w+|прекратил\w*|закончил\w*|бросил\w*|не\s+принима\w+\s+(?:уже|с)", s)
        current = re.search(r"принимает|получает|на\s+фоне|в\s+настоящее\s+время|сейчас|продолжает|назначен\w*", s)
        past = re.search(r"принимал\w*|получал\w*|ранее|раньше|в\s+прошлом|в\s+анамнезе", s)
        days: Optional[int] = None
        m = re.search(r"(\d+)\s*(дн\w*|сут\w*|недел\w*|нед\b|мес\w*|год\w*|лет)\s*(?:назад)?", s)
        if m:
            u = _unit_days(m.group(2))
            if u:
                days = int(m.group(1)) * u
        else:
            m = re.search(r"(один|одна|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+"
                          r"(дн\w*|сут\w*|недел\w*|мес\w*|год\w*|лет)", s)
            if m:
                u = _unit_days(m.group(2))
                if u:
                    days = _NUMWORDS[m.group(1)] * u
            else:
                m = re.search(r"(недел\w+|месяц\w*|год\w*)\s+назад", s)
                if m:
                    u = _unit_days(m.group(1))
                    if u:
                        days = u
        if (stopped or (past and not current)) and days is not None:
            return days, f"Срок после отмены ИМАО взят из текста: {days} сут."
        if (stopped or (past and not current)) and days is None:
            return None, ""
        if current and not stopped:
            return 0, "ИМАО — текущий приём (по тексту)."
    return 0, "Статус приёма ИМАО не указан — трактуется как ТЕКУЩИЙ приём (консервативно), уточните."


# ============================================================ подтверждения врача
CONFIRMATIONS: Dict[str, Dict[str, Any]] = {
    "no_epilepsy_or_organic_cns": {"field": "has_epilepsy_or_organic_cns", "label_ru": "Эпилепсии и органического поражения ЦНС нет"},
    "no_lactase_deficiency": {"field": "has_lactase_deficiency", "label_ru": "Дефицита лактазы / непереносимости галактозы нет"},
    "no_maoi_history": {"field": "maoi_ever", "label_ru": "ИМАО не принимает и не принимал в последние 14 суток"},
    "no_pregnancy_lactation": {"field": "is_pregnant_or_lactating", "label_ru": "Беременности и лактации нет"},
    "no_hepatic_insufficiency": {"field": "hepatic_insufficiency_documented", "label_ru": "Печёночной недостаточности нет"},
    "liver_function_normal": {"field": "child_pugh_class_text", "set": "N",
                              "label_ru": "Функция печени в норме (класс Чайлд-Пью N), анализов в ЭМК нет"},
    "no_suicidal_ideation": {"field": "suicidal_ideation_documented", "label_ru": "Суицидальные мысли оценены: отсутствуют"},
    "no_psychotic_symptoms": {"field": "has_psychotic_symptoms", "label_ru": "Психотической симптоматики нет"},
}


def apply_confirmations(ex: Dict[str, Any], confirmations: Optional[Dict[str, bool]]) -> Dict[str, Any]:
    """Подтверждение врача превращает «неизвестно» в «нет». Противоречие с найденным в тексте не затирается."""
    out = copy.deepcopy(ex)
    if not confirmations:
        return out
    for key, flag in confirmations.items():
        meta = CONFIRMATIONS.get(key)
        if not meta or flag is not True:
            continue
        field = meta["field"]
        bucket = "profile" if field in out["profile"] else "clinical_state"
        cur = out[bucket].get(field)
        if cur is True or (meta.get("set") and cur not in (None, meta["set"])):
            out["warnings"].append(f"Подтверждение «{meta['label_ru']}» противоречит данным ЭМК — оставлено значение из ЭМК.")
            continue
        new_val = meta.get("set", False)
        out[bucket][field] = new_val
        out["evidence"].setdefault(field, []).append(
            {"value": new_val, "text": meta["label_ru"], "source": "doctor_confirmed"})
        if field == "maoi_ever":
            out["profile"]["days_since_maoi"] = None
    return out


def missing_confirmations(ex: Dict[str, Any]) -> List[Dict[str, str]]:
    """Что врачу нужно подтвердить, чтобы неизвестное перестало блокировать зелёный статус."""
    out = []
    for key, meta in CONFIRMATIONS.items():
        field = meta["field"]
        bucket = "profile" if field in ex["profile"] else "clinical_state"
        if ex[bucket].get(field) is None:
            out.append({"key": key, "field": field, "label_ru": meta["label_ru"]})
    return out
