# backend/domain/routing.py — юридическая и клиническая маршрутизация COMPASS-HYBRID v2.6:
# Закон № 3185-1 (ст. 23/29), Приказ № 1071, Kintsugi DAM (вне клинических решений),
# удержание К2. Противопоказания ОХЛП решает ТОЛЬКО декларативный движок
# backend/domain/rules.py — этот модуль решений о препарате не принимает
# (legacy-путь run_full_clinical_compass_pipeline удалён; замороженная реплика для
# регрессионного эталона живёт в benchmarks/ehr_nlp_benchmark.py).
import json
from typing import Any, Dict, List, Optional

from backend.bridges.core import RapiraCCoreBridge
from backend.domain.trajectory import TrajectoryClusterEngine24D
from backend.paths import KB_PATH


class KintsugiVoiceBiomarkerAdapter:
    """Адаптер голосовых биомаркеров (пороговая логика по C-DSP-фичам).

    Честная оговорка (карточка KintsugiHealth/dam, Hugging Face): базовая модель
    обучена на английской речи и не валидирована на русском. Поэтому голосовой
    контур — механика вовлечённости и тренд, он ВНЕ клинических алертов и рекомендаций.
    """

    def analyze_voice_checkin(
        self,
        acoustic_features: Dict[str, float],
        self_reported_phq9: int,
        self_reported_gad7: int,
    ) -> Dict[str, Any]:
        pause_ratio = acoustic_features.get("pause_ratio", acoustic_features.get("silence_ratio", 0.18))
        jitter_pct = acoustic_features.get("jitter_local_pct", 0.8)
        shimmer_pct = acoustic_features.get("shimmer_local_pct", 2.5)

        if pause_ratio >= 0.40 or shimmer_pct >= 6.0:
            dep_class = 2  # Тяжелая депрессия по акустике голоса
        elif pause_ratio >= 0.28 or shimmer_pct >= 4.0:
            dep_class = 1  # Умеренная депрессия
        else:
            dep_class = 0

        if jitter_pct >= 2.2:
            anx_class = 3
        elif jitter_pct >= 1.4:
            anx_class = 2
        elif jitter_pct >= 0.9:
            anx_class = 1
        else:
            anx_class = 0

        dissimulation_detected = (dep_class == 2 and self_reported_phq9 < 10) or (
            anx_class == 3 and self_reported_gad7 < 8
        )

        return {
            "model_source": "KintsugiHealth/dam (Apache 2.0, Whisper-Small multi-task acoustic head)",
            # П1 реестра честности: DAM обучена на английской речи —
            # классы носят ИССЛЕДОВАТЕЛЬСКИЙ характер и не участвуют ни в одном
            # клиническом решении (ни алерты, ни маршрутизация, ни К4).
            "clinical_scope": "EXPERIMENTAL_RESEARCH_ONLY",
            "experimental_voice_depression_class": dep_class,
            "experimental_voice_anxiety_class": anx_class,
            "acoustic_parameters": acoustic_features,
            "dissimulation_flag": dissimulation_detected,
            "dissimulation_warning": (
                "ИССЛЕДОВАТЕЛЬСКИЙ СИГНАЛ (не диагноз): расхождение текстовой анкеты и "
                "акустических параметров речи (языконезависимые C-фичи). Рекомендуется "
                "врачу провести углублённый клинический опрос."
                if dissimulation_detected
                else None
            ),
        }


class ClinicalCompassRuleEngine:
    def __init__(
        self,
        kb_path: str = str(KB_PATH),
        c_bridge: Optional[RapiraCCoreBridge] = None,
    ):
        self.kb_path = kb_path
        self.reload_knowledge_base()
        self.c_bridge = c_bridge or RapiraCCoreBridge()
        self.cluster_engine = TrajectoryClusterEngine24D()
        self.voice_analyzer = KintsugiVoiceBiomarkerAdapter()

    def reload_knowledge_base(self) -> None:
        with open(self.kb_path, "r", encoding="utf-8") as f:
            self.kb = json.load(f)

    def register_custom_scale_plugin(self, scale_code: str, scale_manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Метрика №10: Горячее добавление новой шкалы за 1 клик без изменения ядра."""
        self.kb["psychometric_scales_registry"][scale_code] = scale_manifest
        return {
            "status": "PLUGIN_REGISTERED_HOT_RELOAD",
            "scale_code": scale_code,
            "total_scales_in_registry": len(self.kb["psychometric_scales_registry"]),
        }

    def evaluate_routing_and_alerts(self, clinical_state: Dict[str, Any]) -> Dict[str, Any]:
        # Tri-state: отсутствие ключа или None = «НЕ ОЦЕНЕНО», а не 0. Раньше .get(key, 0) превращал
        # пропущенный п.9 PHQ-9 в «суицидальных мыслей нет» и выдавал зелёный маршрут.
        raw_total = clinical_state.get("phq9_total")
        raw_item9 = clinical_state.get("phq9_item9_suicidal")
        raw_cssrs = clinical_state.get("cssrs_score")
        suicidal_doc = clinical_state.get("suicidal_ideation_documented")  # True / False / None
        phq9_total = raw_total or 0
        phq9_item9 = raw_item9 or 0
        cssrs_score = raw_cssrs or 0
        mdq_score = clinical_state.get("mdq_bipolar_score") or 0
        has_psychosis = bool(clinical_state.get("has_psychotic_symptoms"))
        severe_ae = clinical_state.get("severe_adverse_event", False)
        week_of_therapy = clinical_state.get("week_of_therapy", 0)
        symptom_reduction_pct = clinical_state.get("symptom_reduction_pct", 100.0)
        failed_ssri = bool(clinical_state.get("failed_first_line_ssri", False))
        suicidal_marker = suicidal_doc is True
        risk_assessed = (raw_item9 is not None) or (raw_cssrs is not None) or (suicidal_doc is not None)
        data_gaps: List[Dict[str, str]] = []

        mandatory_reasons: List[str] = []
        recommended_reasons: List[str] = []
        critical_alert = False
        law_3185_article = None
        pharmacovigilance_1071 = None

        # 1. Экстренная маршрутизация «ОБЯЗАТЕЛЬНО к психиатру».
        # Правовая квалификация (ст. 23/29 Закона № 3185-1) — СПРАВОЧНЫЙ КОНТЕКСТ для врача:
        # решение об освидетельствовании, вызове бригады или госпитализации принимает
        # врач-психиатр, система ничего не решает и не вызывает (Условие 3 кейса).
        if cssrs_score >= 4 or has_psychosis:
            critical_alert = True
            law_3185_article = (
                "Правовой контекст для врача-психиатра (справочно): Ст. 29 п. «а» Закона РФ № 3185-1 — "
                "недобровольная госпитализация возможна при непосредственной опасности для себя/окружающих. "
                "Решение о вызове бригады и госпитализации принимает врач-психиатр."
            )
            mandatory_reasons.append(
                f"Маркеры критического риска (C-SSRS={cssrs_score}, психотика={has_psychosis}) — экстренная консультация психиатра."
            )
        elif phq9_item9 >= 1 or cssrs_score >= 1 or phq9_total >= 20 or mdq_score >= 7 or suicidal_marker:
            critical_alert = (phq9_item9 >= 1 or cssrs_score >= 1 or phq9_total >= 20 or suicidal_marker)
            law_3185_article = (
                "Правовой контекст для врача (справочно): Ст. 23 Закона РФ № 3185-1 — психиатрическое "
                "освидетельствование проводится с информированным добровольным согласием. "
                "Система подготовила черновик направления; решение и оформление — за врачом."
            )
            mandatory_reasons.append(
                f"Маркеры высокого риска: PHQ-9={phq9_total} (п.9={phq9_item9}), C-SSRS={cssrs_score}, MDQ(БАР)={mdq_score}"
                + (", в ЭМК документированы суицидальные мысли." if suicidal_marker else ".")
            )

        if severe_ae:
            critical_alert = True
            mandatory_reasons.append("Серьезная нежелательная реакция (подозрение на серотониновый синдром / тризм).")
            pharmacovigilance_1071 = {
                "regulation": "Приказ Росздравнадзора № 1071 от 15.02.2017 и правила GVP ЕАЭС",
                "recipient": "АИС Росздравнадзора + служба фармаконадзора ООО «иФарма» (ГК «ХимРар»)",
                "status": "ЧЕРНОВИК ИЗВЕЩЕНИЯ СФОРМИРОВАН АВТОМАТИЧЕСКИ",
            }

        # 2. Плановая маршрутизация «ЖЕЛАТЕЛЬНО к психиатру»
        if not mandatory_reasons:
            if 10 <= phq9_total <= 19:
                recommended_reasons.append(f"Коморбидная умеренная депрессия (PHQ-9 = {phq9_total} баллов).")
            if week_of_therapy >= 4 and symptom_reduction_pct < 25.0:
                recommended_reasons.append(f"Отсутствие снижения симптоматики >= 25% к {week_of_therapy}-й неделе.")
            if failed_ssri:
                recommended_reasons.append("Непереносимость или неэффективность 1-й линии терапии (СИОЗС) в анамнезе.")
            # П1: голосовые классы DAM в маршрутизацию не попадают (англоязычная модель,
            # experimental) — скрытая диссимуляция ловится правилами К4 по C-DSP-паузам.

        if not risk_assessed:
            data_gaps.append({
                "field": "suicide_risk_assessment",
                "why_needed": "Суицидальный риск не оценён: нет п.9 PHQ-9, C-SSRS и записи о суицидальных мыслях. "
                              "Отсутствие данных не равно отсутствию риска.",
                "source_id": "KR_DEPRESSION_MZ",
            })

        if mandatory_reasons:
            routing_category = "MANDATORY_PSYCHIATRIST"
            ui_badge = {
                "color": "RED_CRITICAL",
                "icon": "[!]",
                "title": "ОБЯЗАТЕЛЬНО НАПРАВЛЕНИЕ К ВРАЧУ-ПСИХИАТРУ",
                "is_emergency_alert": critical_alert,
                "patient_crisis_screen_activated": critical_alert,
                "patient_hotline_contacts": "Единый телефон доверия: 8-800-200-47-03 | Экстренная помощь: 112" if critical_alert else None,
            }
        elif recommended_reasons:
            routing_category = "RECOMMENDED_PSYCHIATRIST"
            ui_badge = {
                "color": "AMBER_WARNING",
                "icon": "[->]",
                "title": "ЖЕЛАТЕЛЬНО НАПРАВЛЕНИЕ К ВРАЧУ-ПСИХИАТРУ (Плановая консультация, ф. 057/у-04)",
                "is_emergency_alert": False,
                "patient_crisis_screen_activated": False,
            }
        elif data_gaps:
            routing_category = "DATA_REQUIRED"
            ui_badge = {
                "color": "AMBER_WARNING",
                "icon": "[?]",
                "title": "ТРЕБУЕТСЯ ОЦЕНКА СУИЦИДАЛЬНОГО РИСКА (п.9 PHQ-9 / C-SSRS): данные не внесены",
                "is_emergency_alert": False,
                "patient_crisis_screen_activated": False,
            }
        else:
            routing_category = "PRIMARY_CARE_MANAGEMENT"
            ui_badge = {
                "color": "GREEN_SAFE",
                "icon": "[OK]",
                "title": "Амбулаторное ведение у лечащего врача (невролога / терапевта)",
                "is_emergency_alert": False,
                "patient_crisis_screen_activated": False,
            }

        return {
            "routing_category": routing_category,
            "critical_alert_triggered": critical_alert,
            "ui_visual_block": ui_badge,
            "law_3185_1_classification": law_3185_article,
            "pharmacovigilance_1071_draft": pharmacovigilance_1071,
            "data_gaps": data_gaps,
            "reasons": mandatory_reasons if mandatory_reasons else recommended_reasons,
        }

    def evaluate_patient_followup_and_adherence(
        self,
        indication: str,
        week: int,
        current_score: float,
        baseline_score: float,
        doses_taken: int,
        doses_scheduled: int,
        kop25_score: int,
        reported_mild_drowsiness_day_1_5: bool = False,
        missed_last_dose: bool = False,
    ) -> Dict[str, Any]:
        pdc_pct = round((doses_taken / max(doses_scheduled, 1)) * 100.0, 1)
        reduction_pct = round(((baseline_score - current_score) / max(baseline_score, 1.0)) * 100.0, 1)

        patient_messages: List[str] = []
        doctor_notes: List[str] = []

        if missed_last_dose:
            patient_messages.append(
                "Инструкция по приему (ОХЛП Авиандр®): Вы отметили пропуск приема таблетки. "
                "Примите следующую таблетку (20 мг) в обычное время за 30 минут до еды. "
                "НЕ принимайте двойную дозу для компенсации пропущенной!"
            )

        if reported_mild_drowsiness_day_1_5:
            # Условие 3: система не решает «продолжать терапию» — цитируем ОХЛП и маршрутируем к врачу.
            patient_messages.append(
                "Лёгкая сонливость или головокружение в первые дни — часто встречающееся и обычно "
                "проходящее явление (инструкция Авиандр®, раздел «Побочные действия»: часто 1–10%). "
                "Пожалуйста, сообщите врачу о самочувствии — он видит ваш статус. "
                "Воздержитесь от вождения автомобиля до исчезновения симптомов."
            )
            doctor_notes.append(
                "Зафиксирована лёгкая сонливость/головокружение первых дней (часто 1–10% по ОХЛП, обычно проходит "
                "самостоятельно). Пациенту отправлена цитата из инструкции и рекомендация сообщить врачу. "
                "Решение о продолжении терапии принимает врач; риск раннего дропаута взят на контроль."
            )

        ref_corridor = self.kb["aviandr_clinical_trials_reference_corridors"]["GAD_36_WEEKS_SECHENOV_2025"]
        return {
            "adherence_pdc_pct": pdc_pct,
            "kop25_score": kop25_score,
            "adherence_status": "HIGH" if (pdc_pct >= 80 and kop25_score >= 70) else "AT_RISK_OF_DROPOUT",
            "symptom_reduction_pct": reduction_pct,
            "reference_trial_comparison": {
                "benchmark_study": ref_corridor["authors"],
                "expected_trajectory_sigh_a": ref_corridor["sigh_a_trajectory"],
                "is_on_track": reduction_pct >= 10.0 or week < 2,
            },
            "patient_retention_nudges": patient_messages,
            "doctor_summary_notes": doctor_notes,
        }
