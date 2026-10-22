# tests/test_domain_guidance.py — фенотипы, дифдиагноз, контрольные точки и
# градация доказательности (контуры 2–3). Прогон: python3 -m tests.test_domain_guidance
import sys

from backend.domain.evidence_xai import EvidenceAndXAIEngine
from backend.domain.phenotypes import (
    detect_phenotypes,
    differential_gad_vs_adaptation,
    monitoring_and_review,
)
from backend.domain.trajectory import TrajectoryClusterEngine24D
from backend.paths import KB_PATH

failures = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    kb = __import__("json").loads(KB_PATH.read_text(encoding="utf-8"))

    # 1. Фенотипы: детекция по тексту и по полям, отсутствие детекции без данных
    hits = detect_phenotypes("Пациент водит авто, после COVID-19 тревога", {}, kb)
    codes = {h["code"] for h in hits}
    check("Фенотип «за рулём» детектируется", "GAD_ACTIVE_DRIVER" in codes)
    check("Пост-COVID фенотип детектируется", "POST_COVID_STRESS_ANXIETY" in codes)
    check("Без маркеров фенотипов нет — карточек нет",
          detect_phenotypes("Осмотр. Жалоб нет.", {}, kb) == [])

    # 2. Дифдиагноз F41.1 vs F43.2: tri-state
    dd = differential_gad_vs_adaptation({"stressor_identified": True, "rumination_present": True,
                                         "functioning_impaired": True, "icd10_code": "F43.2"}, kb)
    check("Дифдиагноз: критерии выполнены -> ADAPTATION", dd["verdict"] == "ADAPTATION_CRITERIA_MET")
    dd2 = differential_gad_vs_adaptation({"icd10_code": "F41.1"}, kb)
    check("Дифдиагноз: нет критериев -> DATA_REQUIRED (не «нет»)", dd2["verdict"] == "DATA_REQUIRED"
          and len(dd2["data_required"]) == 3)
    dd3 = differential_gad_vs_adaptation({"stressor_identified": False, "rumination_present": False,
                                          "functioning_impaired": False}, kb)
    check("Дифдиагноз: все критерии явно «нет» -> картина ГТР", dd3["verdict"] == "GAD_PICTURE")

    # 3. Контрольные точки и триггеры пересмотра
    m = monitoring_and_review({"therapy_day": 3}, kb)
    check("День 3 -> окно недели 2", m["current_checkpoint"] == "week_2")
    m4 = monitoring_and_review({"therapy_day": 30, "baseline_score": 20, "current_score": 18}, kb)
    check("Неделя 4+, снижение 10% -> триггер недостаточного ответа",
          any(t["type"] == "INSUFFICIENT_RESPONSE" for t in m4["review_triggers"]))
    m4b = monitoring_and_review({"therapy_day": 30, "baseline_score": 20, "current_score": 11}, kb)
    check("Неделя 4+, снижение 45% -> триггера нет",
          not any(t["type"] == "INSUFFICIENT_RESPONSE" for t in m4b["review_triggers"]))
    m5 = monitoring_and_review({"therapy_day": 30}, kb)
    check("Неделя 4+ без шкал -> пометка «ответ оценить нельзя» (tri-state)",
          any(t["type"] == "EFFICACY_ASSESSMENT_INCOMPLETE" for t in m5["review_triggers"]))
    m6 = monitoring_and_review({"therapy_day": 10, "severe_adverse_event": True}, kb)
    check("Серьёзное НЯ -> триггер непереносимости",
          any(t["type"] == "INTOLERABILITY" for t in m6["review_triggers"]))

    # 4. Градация доказательности: не завышаем обещания
    ev = EvidenceAndXAIEngine()
    levels = ev.annotate_evidence_levels()
    check("Антидепрессивный эффект = SECONDARY (не монотерапия РДР)",
          levels["antidepressant"]["level"] == "SECONDARY")
    check("Прокогнитивный эффект = PROMISING (TMT p=0.676/0.704)",
          levels["pro_cognitive"]["level"] == "PROMISING")
    check("Противотревожный эффект = PROVEN", levels["anxiolytic"]["level"] == "PROVEN")
    check("Дисклеймер открытых исследований присутствует", "плацебо" in ev.open_label_disclaimer())

    # 5. Коридоры РКИ III и постковида для графиков
    corr = TrajectoryClusterEngine24D.reference_corridors()
    check("Коридор РКИ III (Морозова 2024) с ремиссией 68.42%",
          corr["rct3_meta"]["remission_week32_pct"] == 68.42)
    check("Постковидный коридор с честной пометкой «без плацебо»",
          "без плацебо" in corr["postcovid_meta"]["note"])

    print()
    if failures:
        print(f"[FAIL] {failures}")
        return 1
    print("[OK] Фенотипы, дифдиагноз, контрольные точки и доказательность верифицированы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
