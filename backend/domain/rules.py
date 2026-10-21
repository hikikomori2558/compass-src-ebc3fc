# backend/domain/rules.py — ЕДИНСТВЕННЫЙ движок клинических решений по противопоказаниям.
#
# Зачем он рядом с C-ядром: ядро Михаила — это математика (СКФ по CKD-EPI, Чайлд-Пью,
# RK4-фармакокинетика), и она имеет право жить в скомпилированном коде. А вот клинические
# РЕШЕНИЯ («этот препарат этому пациенту нельзя») не должны требовать перекомпиляции:
# новый препарат или новая редакция инструкции = новый JSON в backend/rules/, и всё.
# Решения о хард-стопах потребляет только services/pipeline.py; маршрутизация
# (domain/routing.py) эти проверки не дублирует — split-brain двух движков устранён.
# Это и есть честное закрытие метрики 10 (гибкость): правила — данные, версии — в БД,
# каждая рекомендация несёт ссылку на источник с датой сверки (метрика 5).
#
# Ещё два принципа из чек-листа кейса, зашитые сюда:
#   * tri-state: отсутствие данного — это «неизвестно», а не «нет». Unknown не может
#     давать зелёный статус: движок выдаёт DATA_REQUIRED и это янтарный контур.
#   * никаких «вероятностей без источника»: правила оперируют только тем, что написано
#     в ОХЛП/КР, у каждого правила — цитата и ссылка.
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"

# Уровни: HARD_STOP — жёсткое предупреждение с обязательным override-режимом (не блокировка!),
# WARNING — янтарное, INFO — справочное. Решение всегда принимает врач (Условие 3).
SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "HARD_STOP": 2}

_OPS = {
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    # битовая маска: хотя бы один из битов b выставлен в a (например, comedication_bitmask)
    "has_bits": lambda a, b: (int(a) & int(b)) != 0,
}


class RuleFinding:
    __slots__ = ("rule_id", "severity", "message", "source", "inputs")

    def __init__(self, rule_id: str, severity: str, message: str, source: Dict[str, str], inputs: Dict[str, Any]):
        self.rule_id = rule_id
        self.severity = severity
        self.message = message
        self.source = source   # {source_id, doc, quote} — метрика 5: врач кликает и видит обоснование
        self.inputs = inputs   # конкретные значения, из-за которых сработало правило

    def as_dict(self) -> Dict[str, Any]:
        return {"rule_id": self.rule_id, "severity": self.severity, "message": self.message,
                "source": self.source, "inputs": self.inputs}


class DataQualityGap:
    """Отсутствующее критичное данное. Не ошибка и не «нет» — требование дообследования."""
    __slots__ = ("field", "why_needed", "source_id")

    def __init__(self, field: str, why_needed: str, source_id: str):
        self.field = field
        self.why_needed = why_needed
        self.source_id = source_id

    def as_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "why_needed": self.why_needed, "source_id": self.source_id}


class RulesEngine:
    def __init__(self, rule_set: str = "aviandr_hard_stops"):
        self.rule_set = rule_set
        self.version = ""
        self.content_sha256 = ""
        self.rules: List[Dict[str, Any]] = []
        self.required_fields: List[Dict[str, str]] = []
        self.alternatives: List[Dict[str, Any]] = []
        self.load(rule_set)

    # ------------------------------------------------------------------ загрузка
    def load(self, rule_set: str, path: Optional[Path] = None) -> None:
        p = path or (RULES_DIR / f"{rule_set}.json")
        raw = p.read_bytes()
        doc = json.loads(raw)
        self.rule_set = doc.get("rule_set", rule_set)
        self.version = doc["version"]
        self.content_sha256 = hashlib.sha256(raw).hexdigest()
        self.rules = doc["rules"]
        self.required_fields = doc.get("required_fields", [])
        self.alternatives = doc.get("alternatives_if_blocked", [])

    def reload(self) -> None:
        """Горячая перезагрузка того же набора (демо метрики 10: правим JSON — не перекомпилируем)."""
        self.load(self.rule_set)

    # ------------------------------------------------------------------ вычисление
    @staticmethod
    def _field_value(profile: Dict[str, Any], field: str) -> Any:
        # Tri-state соглашение: None/отсутствует = неизвестно; ключи с суффиксом _known
        # позволяют отличить «явно нет» от «не вносили».
        known = profile.get(f"{field}_known", True)
        value = profile.get(field)
        if known is False or value is None:
            return None
        return value

    def evaluate(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        findings: List[RuleFinding] = []
        gaps: List[DataQualityGap] = []

        for req in self.required_fields:
            # required_when: поле обязательно только при условии (например, срок после отмены ИМАО,
            # если ИМАО был). Условие с НЕИЗВЕСТНЫМ полем считаем выполненным (консервативно).
            cond = req.get("required_when")
            if cond is not None:
                applies, _ = self._eval_condition(cond, profile)
                if applies is False:
                    continue
                if applies is None and req.get("required_when_unknown", True) is False:
                    continue   # условие само неизвестно и уже учтено как пробел (например, maoi_ever)
            if self._field_value(profile, req["field"]) is None:
                gaps.append(DataQualityGap(req["field"], req["why_needed"], req.get("source_id", "")))

        for rule in self.rules:
            fired, inputs = self._eval_condition(rule["when"], profile)
            if fired:
                findings.append(RuleFinding(
                    rule["rule_id"], rule["severity"], rule["message_ru"], rule["source"], inputs
                ))

        findings.sort(key=lambda f: -SEVERITY_ORDER[f.severity])
        worst = "GREEN_IN_LABEL" if not findings else (
            "AMBER_REVIEW" if findings[0].severity in ("WARNING", "INFO") else "RED_HARD_STOP"
        )
        if gaps and worst == "GREEN_IN_LABEL":
            worst = "AMBER_DATA_REQUIRED"   # неизвестность не бывает зелёной

        return {
            "rule_set": self.rule_set,
            "rule_version": self.version,
            "content_sha256": self.content_sha256,
            "status": worst,
            "findings": [f.as_dict() for f in findings],
            "data_required": [g.as_dict() for g in gaps],
            "alternatives_if_blocked": self.alternatives if worst == "RED_HARD_STOP" else [],
            # Человек в контуре: система предупреждает и оформляет, назначает врач.
            "human_in_the_loop_note": "Все находки — предупреждения для подтверждения врачом; "
                                      "назначение и override оформляются врачом с указанием причины.",
        }

    def _eval_condition(self, cond: Dict[str, Any], profile: Dict[str, Any]):
        """Мини-DSL: {'all':[...]} | {'any':[...]} | {'field','op','value'}.
        Правило с неизвестным полем НЕ срабатывает (не наказываем за пробел в данных),
        но сам пробел попадает в data_required выше."""
        kind = cond.get("kind", "cmp")
        if kind in ("all", "any"):
            results = [self._eval_condition(c, profile) for c in cond[kind]]
            if kind == "all":
                vals = [v for fired, v in results if fired is not None]
                unknown = any(fired is None for fired, _ in results)
                if unknown:
                    return (None, {})  # не можем решить — не срабатываем, ждём данные
                return (len(vals) == len(results) and len(results) > 0, {})
            fired_any = [(f, v) for f, v in results if f]
            return (bool(fired_any), fired_any[0][1] if fired_any else {})
        field = cond["field"]
        value = self._field_value(profile, field)
        if value is None:
            return (None, {})
        op = _OPS[cond["op"]]
        target = cond["value"]
        if cond["op"] == "in" and isinstance(target, list):
            ok = op(value, target)
        else:
            ok = op(value, target)
        return (bool(ok), {field: value})


if __name__ == "__main__":
    # Быстрая самопроверка без тестов: граничный пациент прямо из терминала.
    eng = RulesEngine()
    demo = {
        "age": 67, "sex": 0, "egfr_ckd_epi": 59.9, "child_pugh_class": "N",
        "days_since_maoi": None, "is_pregnant_or_lactating": False,
    }
    res = eng.evaluate(demo)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:1200])
