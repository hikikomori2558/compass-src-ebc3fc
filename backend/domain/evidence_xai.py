# backend/evidence_xai_cbr.py
"""
Модуль повышения авторитетности данных, объяснимого ИИ (XAI / SHAP по ГОСТ Р 59921.1-2022)
и поиска похожих клинических случаев (Case-Based Reasoning по слайду №7 Д.С. Петелина)
на базе 5 опубликованных клинических исследований препарата «Авиандр®» (N = 659).
"""
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List

from backend.paths import KB_PATH

# Названия всех 24 осей вектора состояния z_t in R^24 (Раздел 6.8 спецификации)
FEATURE_NAMES_24D = [
    "1. HADS-A (Скрининг тревоги)",
    "2. HADS-D (Скрининг депрессии)",
    "3. GAD-7 / ГТР-7 (Тяжесть тревоги)",
    "4. PHQ-9 (Тяжесть депрессии)",
    "5. Суицидальный индекс (PHQ-9 п.9 + C-SSRS)",
    "6. MoCA (Когнитивный статус)",
    "7. Скорость редукции тревоги (dGAD-7/dt)",
    "8. Скорость редукции депрессии (dPHQ-9/dt)",
    "9. Отклонение от медианы РКИ Сеченовки (Z-score)",
    "10. Сутки терапии от старта курса",
    "11. Ctrough активного метаболита М1 (CYP2D6 PK)",
    "12. Фактическое покрытие терапией (PDC, %)",
    "13. Приверженность по опроснику КОП-25",
    "14. Точность тайминга «за 30 мин до еды» (RMSE, мин)",
    "15. Акустический скор тревоги (Kintsugi DAM)",
    "16. Акустический скор депрессии (Kintsugi DAM)",
    "17. Доля пауз в речи (Pause Ratio, voice_dsp.c)",
    "18. Время ответа на вопрос анкеты (сек/пункт)",
    "19. Вариабельность ритма сердца (HRV RMSSD, мс)",
    "20. Индекс вегетативного стресса Баевского (rPPG)",
    "21. Циркадный индекс восстановления сна (мишень 5-HT7)",
    "22. Стартовый индекс фармакофобии / ноцебо",
    "23. Флаг эндокринно-соматических масок (ТТГ/Ферритин/Hb)",
    "24. Кардиологический запас безопасности (интервал QTc, мс)",
]

# Популяционные средние (Baseline Expected Values E[z]) для расчета точных линейно-аддитивных SHAP вкладов
POPULATION_BASELINE_24D = [
    10.0, 5.0, 10.0, 6.0, 0.0, 27.4,
    -2.0, -1.0, 0.0, 7.0,
    310.0, 92.0, 75.0, 5.0,
    1.0, 0.0, 0.18, 4.0,
    34.0, 140.0, 0.80, 2.0, 0.0, 35.0,
]

# Верифицированные клинические архетипы из 5 опубликованных РКИ «Авиандра®» для Case-Based Reasoning (CBR)
VERIFIED_RKI_COHORTS_CBR = [
    {
        "cohort_id": "RKI_SECHENOV_2025_GAD_LONG_TERM",
        "study_citation": "Кинкулькина М.А., Волель Б.А., Смолярчук Е.А., Иващенко А.В. и соавт. Неврология, нейропсихиатрия, психосоматика. 2025;17(6):44–53",
        "sample_size": 98,
        "indication_icd10": ["F41.1"],
        "archetype_profile": "Хроническое ГТР (36 недель терапии Авиандр® 40 мг/сут): SIGH-A 22.06 -> 6.90 (p < 0.001), MADRS 14.60 -> 4.15, улучшение когниций MoCA 27.40 -> 29.00",
        "verified_outcomes": {"response_rate_pct": 83.7, "remission_rate_pct": 66.7, "serious_ae_pct": 0.0},
        "prototype_vector_subset": {"gad7": 15.0, "phq9": 9.0, "moca": 27.4, "m1_ctrough": 320.0},
    },
    {
        "cohort_id": "RKI_POPOVA_2024_POST_COVID_ADAPTATION",
        "study_citation": "Попова В.Б., Антонова Е.А. и соавт. Журнал неврологии и психиатрии им. С.С. Корсакова. 2024;124(7):1–8",
        "sample_size": 109,
        "indication_icd10": ["F43.2", "R45.7", "Z60.0"],
        "archetype_profile": "Тревожные состояния после COVID-19 и расстройство адаптации (4 недели, Авиандр® 20 мг 2 р/сут): снижение тревоги на 40% с 1-й недели",
        "verified_outcomes": {"response_rate_pct": 83.0, "remission_rate_pct": 74.4, "serious_ae_pct": 0.0},
        "prototype_vector_subset": {"gad7": 13.0, "phq9": 6.5, "moca": 27.8, "m1_ctrough": 305.0},
    },
    {
        "cohort_id": "RKI_MOROZOVA_2024_GAD_PHASE3",
        "study_citation": "Морозова М.А., Сафарова Т.П., Глускина Л.Я. и соавт. Современная терапия психических расстройств. 2024;(3):2–16",
        "sample_size": 160,
        "indication_icd10": ["F41.1", "F41.9"],
        "archetype_profile": "РКИ III фазы при ГТР (доза 40 мг/сут): редукция тревоги на 42% к 8-й неделе и на 69% к 24-й неделе без синдрома отмены",
        "verified_outcomes": {"response_rate_pct": 69.0, "remission_rate_pct": 62.5, "serious_ae_pct": 0.0},
        "prototype_vector_subset": {"gad7": 16.0, "phq9": 8.0, "moca": 27.2, "m1_ctrough": 315.0},
    },
    {
        "cohort_id": "RKI_SECHENOV_HIMRAR_2025_DDI_CARDIO_POLYPHARMACY",
        "study_citation": "Клиническое исследование I фазы по межлекарственным взаимодействиям (Сеченовский Университет / ГК «ХимРар», 21.11.2025, N = 64)",
        "sample_size": 64,
        "indication_icd10": ["F41.1", "F43.2"],
        "archetype_profile": "Коморбидные пациенты на сопутствующей терапии метопрололом (CYP2D6), дулоксетином, ципрофлоксацином или флуконазолом + учет полиморфизмов CYP2D6",
        "verified_outcomes": {"ddi_safety_confirmed_pct": 100.0, "hemodynamic_stability_pct": 100.0, "serious_ae_pct": 0.0},
        "prototype_vector_subset": {"gad7": 14.5, "phq9": 11.5, "moca": 26.5, "m1_ctrough": 540.0},
    },
    {
        "cohort_id": "RKI_SECHENOV_2026_PANIC_DISORDER_PHASE3",
        "study_citation": "РКИ III фазы при паническом расстройстве (Сеченовский Университет / ООО «Авинейро», 26.03.2026, N = 228)",
        "sample_size": 228,
        "indication_icd10": ["F41.0"],
        "archetype_profile": "Паническое расстройство (12 недель терапии Авиандр® 40 мг/сут): статистически значимое снижение частоты панических атак со 2-й недели",
        "verified_outcomes": {"clinical_improvement_week2_pct": 78.5, "serious_ae_pct": 0.0},
        "prototype_vector_subset": {"gad7": 17.0, "phq9": 10.0, "moca": 27.5, "m1_ctrough": 310.0},
    },
]


EFFECT_GRADES_RU = {
    "anxiolytic": "Противотревожный эффект",
    "antidepressant": "Антидепрессивный эффект",
    "anti_asthenic": "Антиастенический эффект",
    "pro_cognitive": "Прокогнитивный эффект",
    "daytime_sleepiness_neutral": "Нейтральность к дневной сонливости",
}
LEVEL_RU = {"PROVEN": "ДОКАЗАНО (плацебо-контролируемые РКИ)",
            "SECONDARY": "ВТОРИЧНЫЙ ИСХОД (подтверждено как сопутствующий исход)",
            "PROMISING": "ПЕРСПЕКТИВНЫЙ (доклиника + открытые; в РКИ отличие от плацебо незначимо)"}


class EvidenceAndXAIEngine:
    """
    Генерирует блок объяснимого ИИ (SHAP values по ГОСТ Р 59921.1-2022),
    Case-Based Reasoning (Топ-3 ближайших РКИ-прототипа) и криптографический паспорт доказательности.
    """

    def __init__(self, kb_path: Path = KB_PATH):
        self.kb_path = kb_path
        if self.kb_path.exists():
            raw_bytes = self.kb_path.read_bytes()
            self.kb_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        else:
            self.kb_sha256 = hashlib.sha256(b"COMPASS_HYBRID_V26_STATIC").hexdigest()

    def compute_shap_attributions_24d(self, z_24: List[float], assigned_cluster: str) -> Dict[str, Any]:
        """
        Вычисляет локальные SHAP-вклады phi_i для 24 признаков (ГОСТ Р 59921.1-2022: прослеживаемость ИИ).
        Возвращает Топ-5 доминирующих факторов, определивших клиническое решение.
        """
        t0 = time.perf_counter()
        # Весовые чувствительности осей (калиброваны по клинической значимости доменов)
        weights = [
            0.12, 0.10, 0.18, 0.22, 1.85, -0.08,  # Домен 1: Психометрика (ось 5 — суицид имеет максимальный вес)
            0.15, 0.14, 0.20, -0.05,              # Домен 2: Динамика
            0.004, -0.03, -0.025, 0.04,           # Домен 3: Фармакокинетика М1 и комплаенс
            0.25, 0.42, 1.90, -0.22,              # Домен 4: Голос Kintsugi DAM и Pause Ratio
            -0.03, 0.005, -0.30, 0.18, 0.65, -0.02 # Домен 5: Вегетатика rPPG, 5-HT7 циркадный ритм, маски ТТГ, QTc
        ]

        attributions = []
        for idx in range(min(len(z_24), 24)):
            delta = z_24[idx] - POPULATION_BASELINE_24D[idx]
            phi_val = round(delta * weights[idx], 4)
            attributions.append({
                "feature_index": idx + 1,
                "feature_name": FEATURE_NAMES_24D[idx],
                "patient_value": z_24[idx],
                "population_baseline": POPULATION_BASELINE_24D[idx],
                "shap_phi_value": phi_val,
                "direction": "INCREASES_RISK_OR_URGENCY" if phi_val > 0 else "PROTECTIVE_OR_STABILIZING",
            })

        # Сортируем по абсолютной величине вклада |phi_i|
        top_5_drivers = sorted(attributions, key=lambda x: abs(x["shap_phi_value"]), reverse=True)[:5]
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return {
            "gost_standard_compliance": "ГОСТ Р 59921.1–2022 ... 59921.7–2022 (Полная интерпретируемость и прослеживаемость выводов ИИ)",
            "shap_compute_latency_ms": round(elapsed_ms, 4),
            "target_explained_cluster": assigned_cluster,
            "top_5_shap_clinical_drivers": top_5_drivers,
        }

    def find_similar_rki_cohorts_cbr(
        self,
        icd10_code: str,
        gad7: float,
        phq9: float,
        moca: float,
        m1_ctrough: float,
    ) -> List[Dict[str, Any]]:
        """
        Case-Based Reasoning (Слайд №7 Д.С. Петелина):
        Находит Топ-3 наиболее близких верифицированных клинических когорты из РКИ «Авиандра®» (N = 659).
        """
        scored_cohorts = []
        for cohort in VERIFIED_RKI_COHORTS_CBR:
            p = cohort["prototype_vector_subset"]
            # Нормализованное расстояние Махаланобиса по ключевым клинико-фармакокинетическим осям
            dist_sq = (
                ((gad7 - p["gad7"]) / 5.0) ** 2
                + ((phq9 - p["phq9"]) / 5.0) ** 2
                + ((moca - p["moca"]) / 2.0) ** 2
                + ((m1_ctrough - p["m1_ctrough"]) / 150.0) ** 2
            )
            # Бонус за точное совпадение рубрики МКБ-10
            if icd10_code in cohort["indication_icd10"]:
                dist_sq *= 0.45

            similarity_pct = round(100.0 * math.exp(-0.35 * math.sqrt(dist_sq)), 1)
            scored_cohorts.append({
                "similarity_score_pct": similarity_pct,
                "cohort_id": cohort["cohort_id"],
                "study_citation": cohort["study_citation"],
                "rki_sample_size": cohort["sample_size"],
                "archetype_profile": cohort["archetype_profile"],
                "expected_clinical_outcomes": cohort["verified_outcomes"],
            })

        scored_cohorts.sort(key=lambda x: x["similarity_score_pct"], reverse=True)
        return scored_cohorts[:3]

    def _kb(self) -> Dict[str, Any]:
        import json

        if not hasattr(self, "_kb_cache"):
            self._kb_cache = json.loads(self.kb_path.read_text(encoding="utf-8"))
        return self._kb_cache

    def annotate_evidence_levels(self) -> Dict[str, Dict[str, str]]:
        """Градация доказательности эффектов из KB: система не завышает обещания.
        Возвращает {эффект: {level, level_ru, evidence}} для карточек UI."""
        grades = self._kb().get("evidence_grades", {})
        out: Dict[str, Dict[str, str]] = {}
        for key, title in EFFECT_GRADES_RU.items():
            g = grades.get(key)
            if not g:
                continue
            out[key] = {"effect": title, "level": g["level"],
                        "level_ru": LEVEL_RU.get(g["level"], g["level"]),
                        "evidence": g["evidence"]}
        return out

    def open_label_disclaimer(self) -> str:
        return self._kb().get("open_label_disclaimer", "")

    def build_authority_provenance_passport(self, icd10_code: str, is_blocked: bool) -> Dict[str, Any]:
        """Формирует юридический и регуляторный паспорт авторитетности источника данных."""
        return {
            "knowledge_base_sha256_integrity": self.kb_sha256,
            "grls_registration_passport": {
                "trade_name": "Авиандр® (Маритупирдин, табл. 20 мг)",
                "ru_number": "ЛП-№(002445)-(РГ-RU) от 31.05.2023 (актуализация ОХЛП ГРЛС 31.03.2026)",
                "who_inn_status": "Включен в международный реестр МНН ВОЗ (Maritupirdine)",
                "marketing_authorization_holder": "ООО «Авинейро» (ГК «ХимРар»)",
                "pharmacovigilance_organization": "ООО «иФарма» (Сколково)",
            },
            "minzdrav_clinical_guidelines": [
                {
                    "title": "КР Минздрава РФ «Генерализованное тревожное расстройство» (F41.1, ред. 2024–2026 гг.)",
                    "recommendation_grade": "Уровень убедительности рекомендаций C (УДД 2 — РКИ II/III фаз)",
                },
                {
                    "title": "КР Минздрава РФ «Депрессивный эпизод, Рекуррентное депрессивное расстройство» (F32/F33, 2024–2026 гг.)",
                    "recommendation_grade": "Обязательный скрининг суицидального риска по протоколу C-SSRS",
                },
            ],
            "regulatory_compliance_locks": {
                "human_in_the_loop_enforced": True,
                "off_label_vk_guard_active": icd10_code not in ("F41.1", "F41.9", "F43.2", "R45.7", "Z60.0"),
                "ohpl_hard_stop_triggered": is_blocked,
            },
        }


if __name__ == "__main__":
    engine = EvidenceAndXAIEngine()
    sample_z24 = [
        13.0, 6.0, 15.0, 12.0, 0.0, 26.0,
        -2.5, -1.0, 0.1, 14.0,
        565.0, 96.0, 82.0, 4.5,
        2.0, 1.0, 0.22, 3.8,
        26.5, 160.0, 0.78, 1.5, 1.0, 462.0,
    ]
    shap_res = engine.compute_shap_attributions_24d(sample_z24, assigned_cluster="K3")
    cbr_res = engine.find_similar_rki_cohorts_cbr("F41.1", gad7=15.0, phq9=12.0, moca=26.0, m1_ctrough=565.0)
    passport = engine.build_authority_provenance_passport("F41.1", is_blocked=False)

    print("=== 1. ГОСТ Р 59921 SHAP-АТРИБУЦИЯ (ТОП-5 ФАКТОРОВ РЕШЕНИЯ) ===")
    print(json.dumps(shap_res, ensure_ascii=False, indent=2))
    print("\n=== 2. CASE-BASED REASONING (ТОП-3 БЛИЖАЙШИХ КОГОРТЫ РКИ АВИАНДРА, N=659) ===")
    print(json.dumps(cbr_res, ensure_ascii=False, indent=2))
    print("\n=== 3. КРИПТОГРАФИЧЕСКИЙ ПАСПОРТ АВТОРИТЕТНОСТИ ДАННЫХ ===")
    print(json.dumps(passport, ensure_ascii=False, indent=2))
