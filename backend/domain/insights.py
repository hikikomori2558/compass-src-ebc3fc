# backend/domain/insights.py — аналитика дневника: «как препарат действует
# ИМЕННО на этого человека». Врач получает факты, не вероятности:
#   * транзиторная адаптация (сонливость дни 1–5) — ожидаема, совпадает с РКИ;
#   * связь пропусков/времени приёма с динамикой шкал;
#   * тренд GAD-7 против коридора РКИ (ответ ≥25% к 4-й неделе);
#   * побочки после дня X — что типично, что нет;
#   * RMSE от правила «за 30 минут до еды».
# Каждый инсайт помечен evidence-строкой с источником. Решение — за врачом.
from typing import Any, Dict, List

from backend.domain.side_effects import GUIDANCE


def _rmse_meal_30min(rows: List[Dict[str, Any]]) -> float | None:
    """Насколько время приёма отклоняется от правила «за 30 минут до еды».
    before_meal_30min bool; intake_time — фактическое время."""
    marks = [r["before_meal_30min"] for r in rows if r.get("before_meal_30min") is not None]
    if not marks:
        return None
    ok = sum(1 for m in marks if m)
    return round(100.0 * ok / len(marks), 1)


def build_insights(*, therapy_day: int | None, diary: List[Dict[str, Any]],
                   scales: List[Dict[str, Any]], alerts: List[Dict[str, Any]],
                   adherence_pct: float | None) -> Dict[str, Any]:
    facts: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    # --- 1. побочки по дням терапии: транзиторная адаптация vs затяжная
    se_by_day: Dict[str, List[int]] = {}
    for d in diary:
        day = d.get("therapy_day")
        for se in d.get("side_effects", []):
            code = se.get("code")
            if code:
                se_by_day.setdefault(code, []).append(day or 0)
    for code, days in se_by_day.items():
        g = GUIDANCE.get(code, {})
        first = min(days) if days else None
        if code == "sleepy" and days and all(1 <= d <= 5 for d in days):
            facts.append({
                "fact": f"Сонливость только в дни {min(days)}–{max(days)} терапии",
                "verdict": "ОЖИДАЕМАЯ ТРАНЗИТОРНАЯ АДАПТАЦИЯ (H1/α2): совпадает с окном РКИ, "
                           "у большинства регрессирует к 7 дню. Не отменять; вождение — нет.",
                "evidence": "РКИ II Ivashchenko 2021: сонливость 2.4% на 40 мг/сут; ОХЛП 1–10%",
            })
        elif code in ("sleepy", "asthenia") and days and max(days) > 14:
            warnings.append({
                "fact": f"{g.get('title', code)} сохраняется на день {max(days)} (за пределами окна адаптации)",
                "verdict": "Затяжная седация — кандидат на пересмотр: проверить DDI (CYP2D6), "
                           "рассмотреть вечерний приём/снижение дозы. Решение — врач.",
                "evidence": "ОХЛП: частые НЯ обычно проходят самостоятельно",
            })
        if g.get("always_critical"):
            warnings.append({
                "fact": f"{g.get('title')}: отмечено в дневнике (день {first})",
                "verdict": g["do_now"][0],
                "evidence": "Серотонинергическая комбинация — редкое опасное состояние",
            })

    # --- 2. время приёма и правило «за 30 минут до еды»
    rmse = _rmse_meal_30min(diary)
    if rmse is not None:
        if rmse >= 80:
            facts.append({"fact": f"Правило «за 30 мин до еды» соблюдается в {rmse}% приёмов",
                          "verdict": "Комплаентность по режиму высокая — диспепсия маловероятна",
                          "evidence": "ОХЛП: приём за 30 минут до еды"})
        else:
            facts.append({"fact": f"Правило «за 30 мин до еды» соблюдается только в {rmse}% приёмов",
                          "verdict": "Если есть тошнота/дискомфорт — сначала исправить режим приёма, "
                                     "потом менять препарат",
                          "evidence": "ОХЛП: режим приёма — часть эффективности и переносимости"})

    # --- 3. пропуски доз и приверженность
    if adherence_pct is not None:
        if adherence_pct < 80:
            warnings.append({"fact": f"Приверженность {adherence_pct:.0f}% (< 80%)",
                             "verdict": "Перед выводом о неэффективности исключить пропуски: "
                                        "спросить о забытыx дозах, отметить в дневнике",
                             "evidence": "Триггер пересмотра терапии, слайд 24"})
        else:
            facts.append({"fact": f"Приверженность {adherence_pct:.0f}%",
                          "verdict": "Достаточная для оценки эффективности на контрольной точке",
                          "evidence": "Слайд 24: неделя 4 — ответ и приверженность"})

    # --- 4. тренд шкал против коридора РКИ (контрольная точка 4-й недели)
    gad = sorted([s for s in scales if s.get("scale_code") == "GAD-7"],
                 key=lambda s: s.get("taken_at") or s.get("taken_at") is None)
    if len(gad) >= 2:
        first, last = gad[0]["total_score"], gad[-1]["total_score"]
        if first > 0:
            red = (first - last) / first * 100.0
            if red >= 25:
                facts.append({"fact": f"GAD-7 {first} → {last} (−{red:.0f}%)",
                              "verdict": "Динамика в коридоре ответа (≥25% к 4-й неделе) — курс работает",
                              "evidence": "Слайд 24 + РКИ III (ответ ≥50% к 8 нед — 36.97%)"})
            else:
                warnings.append({"fact": f"GAD-7 {first} → {last} (−{red:.0f}%, < 25%)",
                                 "verdict": "Недостаточный ранний ответ. Чек-лист: приверженность → "
                                            "диагноз → коморбидность → стратегия/психиатр",
                                 "evidence": "Триггер пересмотра (слайд 24), 4-я неделя"})

    # --- 5. суицидальные флаги — всегда наверх, немедленно
    for a in alerts:
        if a.get("critical") and not a.get("doctor_reaction_at"):
            warnings.insert(0, {"fact": f"НЕПОДТВЕРЖДЁННЫЙ критический алерт #{a['alert_id']}",
                                 "verdict": "Требует подтверждения врачом немедленно (Условия 5–6)",
                                 "evidence": "Transactional Outbox: доставка гарантирована"})

    streak = _streak(diary)
    return {
        "facts": facts,
        "warnings": warnings,
        "streak_days": streak,
        "rmse_meal_30min_pct": rmse,
        "human_note": "Инсайты — подсветка фактов врачу. Интерпретация и решение — только врач.",
    }


def _streak(diary: List[Dict[str, Any]]) -> int:
    """Стрик: сколько ПОСЛЕДНИХ дней подряд отмечен приём (taken=true)."""
    days = sorted({d["entry_date"] for d in diary if d.get("taken")}, reverse=True)
    if not days:
        return 0
    streak = 1
    for prev, cur in zip(days, days[1:]):
        if (prev - cur).days == 1:
            streak += 1
        else:
            break
    return streak

