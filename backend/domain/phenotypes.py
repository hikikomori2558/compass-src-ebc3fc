# backend/domain/phenotypes.py — детекция 6 целевых клинических фенотипов,
# дифференциальная диагностика ГТР vs расстройство адаптации и контрольные
# точки терапии (слайд 24). Всё детерминированно: подсказки врачу из базы
# знаний, без «предсказаний». Tri-state: нет данных — нет вывода, а не «нет».
from typing import Any, Dict, List


def detect_phenotypes(ehr_text: str, fields: Dict[str, Any],
                      kb: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Возвращает карточки сработавших фенотипов из kb['patient_phenotypes'].
    Детекция — по маркерам текста ЭМК (нижний регистр) и/или извлечённым полям."""
    text = (ehr_text or "").lower()
    found: List[Dict[str, Any]] = []
    for code, spec in kb.get("patient_phenotypes", {}).items():
        hit_text = any(marker in text for marker in spec.get("detect", {}).get("any_text", []))
        hit_field = any(
            fields.get(k) == v
            for k, v in spec.get("detect", {}).get("fields", {}).items()
            if fields.get(k) is not None
        )
        if hit_text or hit_field:
            found.append({
                "code": code,
                "title": spec["title"],
                "advisory": spec["advisory"],
                "detected_by": ("текст ЭМК" if hit_text else "поле анамнеза"),
            })
    return found


def differential_gad_vs_adaptation(fields: Dict[str, Any],
                                   kb: Dict[str, Any]) -> Dict[str, Any]:
    """Разграничение F41.1 (6B00) и F43.2 (6B43). Tri-state: отсутствующие
    критерии попадают в data_required, а не трактуются как «нет»."""
    dd = kb.get("differential_diagnosis", {}).get("gad_vs_adaptation", {})
    required = [
        ("stressor_identified", "идентифицируемый психосоциальный стрессор"),
        ("rumination_present", "фиксация на стрессоре (руминации)"),
        ("functioning_impaired", "нарушение повседневного функционирования"),
    ]
    present, gaps = [], []
    for key, label in required:
        if fields.get(key) is True:
            present.append(label)
        elif fields.get(key) is False:
            present.append(None)  # явно отмечено отсутствие — не пробел
        else:
            gaps.append({"field": key, "why_needed": f"критерий расстройства адаптации: {label}",
                         "source_id": "DIFF_GAD_F43"})
    criteria_met = all(fields.get(k) is True for k, _ in required)
    icd = fields.get("icd10_code")
    result: Dict[str, Any] = {
        "codes": {"gad": dd.get("gad", {}), "adaptation": dd.get("adaptation", {})},
        "criteria_present": [x for x in present if x is not None],
        "data_required": gaps,
    }
    if criteria_met:
        result["advisory"] = (
            "Критерии расстройства адаптации выполнены. ПРАВИЛО: если клиническая картина "
            "полностью удовлетворяет критериям ГТР, диагноз расстройства адаптации "
            "дополнительно не выставляется."
        )
        result["verdict"] = "ADAPTATION_CRITERIA_MET"
    elif gaps:
        result["advisory"] = ("Для разграничения ГТР (F41.1/6B00) и расстройства адаптации "
                              "(F43.2/6B43) не хватает критериев — они перечислены в data_required.")
        result["verdict"] = "DATA_REQUIRED"
    else:
        result["advisory"] = "Критерии расстройства адаптации не выполнены — картина соответствует ГТР."
        result["verdict"] = "GAD_PICTURE"
    if icd:
        result["documented_icd10"] = icd
    return result


def monitoring_and_review(clinical_state: Dict[str, Any],
                          kb: Dict[str, Any]) -> Dict[str, Any]:
    """Календарь визитов (Визит 0 / Нед 2 / Нед 4 / Нед 8) и ДЕТЕРМИНИРОВАННЫЕ
    триггеры пересмотра терапии. Никаких вероятностей: только пороговые правила."""
    schedule = kb.get("monitoring_schedule", {})
    day = clinical_state.get("therapy_day")
    try:
        day_f = float(day) if day is not None else None
    except (TypeError, ValueError):
        day_f = None

    if day_f is None:
        current_visit = None
        visit_note = "Сутки от старта курса не указаны — контрольная точка не определена."
    elif day_f < 0.5:
        current_visit, visit_note = "visit_0_baseline", "Исходный визит: верификация диагноза и исключение противопоказаний."
    elif day_f < 14:
        current_visit, visit_note = "week_2", "Окно недели 2: ранняя динамика, сон, переносимость."
    elif day_f < 28:
        current_visit, visit_note = "week_4", "Окно недели 4: клинический ответ, функционирование, приверженность."
    else:
        current_visit, visit_note = "week_8", "Окно недели 8: итог ответа и решение о продолжении."

    triggers: List[Dict[str, Any]] = []
    tt = kb.get("therapy_review_triggers", {})

    # 1. Неприемлемая переносимость
    if clinical_state.get("severe_adverse_event") is True:
        triggers.append({
            "type": "INTOLERABILITY",
            "reason": clinical_state.get("adverse_event_note") or "клинически значимое НЯ",
            "action": tt.get("intolerance", {}).get("action",
                      "пересмотреть схему, проверить DDI, при серьёзной НР — фармаконадзор"),
        })

    # 2. Недостаточная эффективность: к 4-й неделе снижение < 25%.
    #    Tri-state: нет базовой или текущей шкалы — нет триггера, но есть пометка.
    baseline = clinical_state.get("baseline_score")
    current = clinical_state.get("current_score")
    reduction = None
    if baseline and current is not None and float(baseline) > 0:
        reduction = (float(baseline) - float(current)) / float(baseline) * 100.0
    if day_f is not None and day_f >= 28:
        if reduction is None:
            triggers.append({
                "type": "EFFICACY_ASSESSMENT_INCOMPLETE",
                "reason": "к 4-й неделе нет пары «исходный/текущий» балл — ответ оценить нельзя",
                "action": "внести SIGH-A/HAM-A исходно и сейчас; без этого решение о продолжении не принимать",
            })
        elif reduction < 25.0:
            triggers.append({
                "type": "INSUFFICIENT_RESPONSE",
                "reason": f"снижение тревоги к 4-й неделе {reduction:.0f}% (< 25%)",
                "action": "; ".join(tt.get("insufficient_response", {}).get("doctor_checklist", [])),
            })

    return {
        "current_checkpoint": current_visit,
        "checkpoint_note": visit_note,
        "schedule": schedule,
        "review_triggers": triggers,
        "human_in_the_loop_note": "Контрольные точки и триггеры — напоминания врачу; решение о пересмотре терапии принимает только врач.",
    }
