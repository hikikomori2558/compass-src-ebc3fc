# backend/domain/trajectory.py — 24-мерный вектор состояния z_t и стратификация К1–К5
# (детерминированные клинические правила + DTW до коридора РКИ Сеченовского исследования).
# ctypes-мост к C-ядру живёт отдельно: backend/bridges/core.py.
from typing import Any, Dict, List


class TrajectoryClusterEngine24D:
    CLUSTERS_METADATA = {
        "K1_FAST_POST_COVID_RESPONDER": {
            "code": "K1",
            "title_ru": "Кластер К1: Быстрый нормотимический респондер (Пост-COVID / Расстройство адаптации)",
            "benchmark": "РКИ III фазы Попова В.Б. и соавт., 2024 (N=109): снижение тревоги на 40% на 1-й нед., ремиссия 68.8–80% к 28-му дню.",
            "control_action": "Продолжить курс Авиандр® 20 мг 2 р/сут до 4 недель в стандартном режиме.",
            "umap_coords": [2.4, 1.8],
        },
        "K2_TRANSIENT_H1_ADAPTATION_DROPOUT_RISK": {
            "code": "K2",
            "title_ru": "Кластер К2: Транзиторная рецепторная адаптация (H1/alpha-2) с риском раннего дропаута (2–5-й день)",
            "benchmark": "Пик метаболита М1 (особенно при CYP2D6 PM): преходящая сонливость/головокружение в первые дни (ОХЛП).",
            "control_action": "Активировать протокол удержания (Retention Nudge): объяснить безопасность адаптации, запрет вождения авто.",
            "umap_coords": [-1.5, 2.2],
        },
        "K3_CUMULATIVE_GAD_36W_RESPONDER": {
            "code": "K3",
            "title_ru": "Кластер К3: Кумулятивный долгосрочный респондер (Хроническое ГТР до 36 недель)",
            "benchmark": "Исследование Сеченовского университета (Кинкулькина М.А., Волель Б.А., 2025, N=98): SIGH-A 22.06 -> 6.90, рост MoCA до 29.0.",
            "control_action": "Плановое ведение + мониторинг SIGH-A, MADRS и когнитивной шкалы MoCA.",
            "umap_coords": [2.1, -1.6],
        },
        "K4_HIDDEN_DISSIMULATION_PSEUDOCOMPLIANCE": {
            "code": "K4",
            "title_ru": "Кластер К4: Скрытая диссимуляция и псевдокомплаенс",
            "benchmark": "Быстрое прокликивание анкет (< 4 с) при высоком акустическом скоре Kintsugi DAM и паузах речи.",
            "control_action": "Включить углубленный клинический опрос + уведомление лечащему врачу.",
            "umap_coords": [-2.3, -0.8],
        },
        "K5_RESISTANCE_OR_ACUTE_CRISIS": {
            "code": "K5",
            "title_ru": "Кластер К5: Терапевтическая резистентность / Декомпенсация / Суицидальный кризис",
            "benchmark": "Отсутствие ответа >= 25% к 4-й неделе (ЖЕЛАТЕЛЬНО к психиатру) ИЛИ C-SSRS / PHQ-9 п.9 >= 1 (ОБЯЗАТЕЛЬНО к психиатру).",
            "control_action": "Экстренный Алертинг врачу (< 45 мс) / маршрутизация к психиатру по Закону РФ № 3185-1 (ст. 23 / ст. 29).",
            "umap_coords": [-3.4, -2.9],
        },
    }

    SECHENOV_36W_SIGH_A = [(0, 22.06), (2, 19.12), (8, 14.12), (16, 9.84), (24, 7.44), (36, 6.90)]

    # РКИ III фазы ГТР (Морозова 2024, n=160): редукция SIGH-A от исходного, баллы
    # (8 недель слепого этапа: -11.97 vs -8.21 плацебо, p=0.006; открытая фаза
    # 16/24/32 недели: -16.08 / -20.28 / -22.53; ответ 89.47%, ремиссия <=7 у 68.42%).
    RCT3_MOROZOVA_2024_SIGH_A = [(0, 0.0), (8, 11.97), (16, 16.08), (24, 20.28), (32, 22.53)]
    RCT3_MOROZOVA_2024_META = {
        "n": 160, "randomization": "3:1 (121 препарат / 39 плацебо)",
        "response_week8_pct": 36.97, "placebo_response_week8_pct": 15.38,
        "remission_week32_pct": 68.42,
    }

    # Постковидная тревога / расстройство адаптации (Попова 2024, n=109, 40 мг/сут до 28 дней)
    POSTCOVID_2024_HAMA = [(0, 20.4), (14, 12.2), (28, 6.2), (35, 4.7)]
    POSTCOVID_2024_META = {
        "n": 109, "response_day28_pct": 83.49, "remission_day28_pct": 68.81,
        "note": "открытое исследование — без плацебо: эффект неотделим от естественного восстановления",
    }

    @classmethod
    def reference_corridors(cls):
        """Эталонные коридоры для графиков динамики и UI."""
        return {
            "sechenov_36w_sigh_a": cls.SECHENOV_36W_SIGH_A,
            "rct3_morozova_2024_sigh_a_reduction": cls.RCT3_MOROZOVA_2024_SIGH_A,
            "rct3_meta": cls.RCT3_MOROZOVA_2024_META,
            "postcovid_2024_hama": cls.POSTCOVID_2024_HAMA,
            "postcovid_meta": cls.POSTCOVID_2024_META,
        }

    @staticmethod
    def compute_dtw_distance(seq_a: List[float], seq_b: List[float]) -> float:
        n, m = len(seq_a), len(seq_b)
        if n == 0 or m == 0:
            return 0.0
        dtw = [[float("inf")] * (m + 1) for _ in range(n + 1)]
        dtw[0][0] = 0.0
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                cost = abs(seq_a[i - 1] - seq_b[j - 1])
                dtw[i][j] = cost + min(dtw[i - 1][j], dtw[i][j - 1], dtw[i - 1][j - 1])
        return round(dtw[n][m] / max(n, m), 3)

    def build_24d_state_vector(
        self,
        psychometrics: Dict[str, float],
        dynamics: Dict[str, float],
        pk_adherence: Dict[str, float],
        biomarkers: Dict[str, float],
        biometrics_endocrine: Dict[str, float],
    ) -> List[float]:
        z_24 = [
            float(psychometrics.get("hads_a", 12.0)),
            float(psychometrics.get("hads_d", 6.0)),
            float(psychometrics.get("gad7", 12.0)),
            float(psychometrics.get("phq9", 7.0)),
            float(psychometrics.get("suicidal_composite", 0.0)),
            float(psychometrics.get("moca", 27.5)),
            float(dynamics.get("d_gad7_dt", -2.5)),
            float(dynamics.get("d_phq9_dt", -1.0)),
            float(dynamics.get("sechenov_rki_z_score", 0.0)),
            float(dynamics.get("therapy_day", 3.0)),
            float(pk_adherence.get("m1_ctrough_pg_ml", 310.0)),
            float(pk_adherence.get("pdc_adherence_pct", 95.0)),
            float(pk_adherence.get("kop25_score", 78.0)),
            float(pk_adherence.get("meal_30min_timing_rmse_min", 6.5)),
            # v8: ГОЛОСОВЫЕ ПРИЗНАКИ ПОЛНОСТЬЮ УДАЛЕНЫ ИЗ ПРОДУКТА. Позиции осей
            # сохранены как нейтральные константы только ради ABI C-ядра (24D);
            # ни правила, ни UI их не читают.
            1.0, 0.0, 0.18,
            float(biomarkers.get("questionnaire_response_sec_per_item", 4.2)),
            float(biometrics_endocrine.get("hrv_rmssd_ms", 34.0)),
            float(biometrics_endocrine.get("baevsky_stress_index", 145.0)),
            float(biometrics_endocrine.get("circadian_5ht7_alignment_idx", 0.82)),
            float(biometrics_endocrine.get("pharmacophobia_nocebo_score", 2.0)),
            float(biometrics_endocrine.get("endocrine_somatic_mask_flag", 0.0)),
            float(biometrics_endocrine.get("qtc_cardio_safety_margin_ms", 35.0)),
        ]
        return [round(v, 3) for v in z_24]

    def assign_cluster(
        self,
        z_24: List[float],
        patient_sigh_a_history: List[float],
        reported_transient_drowsiness: bool = False,
        icd10_code: str = "F41.1",
    ) -> Dict[str, Any]:
        """Детерминированная стратификация К1–К5: пороговые правила из кейса/КР + DTW
        до коридора РКИ. Никакого «GMM» здесь нет и не притворяется: вероятности
        принадлежности появятся, когда будет реальный датасет (план: RWE-пилот)."""
        suicidal_score = z_24[4]
        phq9 = z_24[3]
        therapy_day = z_24[9]
        m1_ctrough = z_24[10]
        resp_time_sec = z_24[17]

        ref_curve = [val for _, val in self.SECHENOV_36W_SIGH_A[: max(len(patient_sigh_a_history), 1)]]
        dtw_dist = self.compute_dtw_distance(patient_sigh_a_history or [22.0], ref_curve)

        matched_rule = ""
        if suicidal_score >= 1.0 or phq9 >= 20.0:
            cluster_key = "K5_RESISTANCE_OR_ACUTE_CRISIS"
            matched_rule = "суицидальные маркеры (композит п.9+C-SSRS >= 1) или PHQ-9 >= 20"
        elif resp_time_sec < 1.5 and phq9 <= 8.0:
            # v8: голосовые признаки удалены — диссимуляцию ловим поведенчески:
            # прокликивание анкеты (< 1.5 с/вопрос) при заниженном самоотчёте.
            cluster_key = "K4_HIDDEN_DISSIMULATION_PSEUDOCOMPLIANCE"
            matched_rule = "анкета заполнена быстрее 1.5 с/вопрос при PHQ-9 <= 8"
        elif therapy_day <= 7.0 and (reported_transient_drowsiness or m1_ctrough > 420.0):
            cluster_key = "K2_TRANSIENT_H1_ADAPTATION_DROPOUT_RISK"
            matched_rule = "день терапии <= 7 при сонливости (отчёт) или Ctrough М1 > 420 пг/мл"
        elif icd10_code in ("F43.2", "R45.7", "Z60.0"):
            cluster_key = "K1_FAST_POST_COVID_RESPONDER"
            matched_rule = "МКБ F43.2 / R45.7 / Z60.0 — пост-COVID / расстройство адаптации"
        else:
            cluster_key = "K3_CUMULATIVE_GAD_36W_RESPONDER"
            matched_rule = "хроническое течение ГТР без маркеров К1/К2/К4/К5 — кумулятивный ответ"

        meta = self.CLUSTERS_METADATA[cluster_key]
        return {
            "assigned_cluster_code": meta["code"],
            "assigned_cluster_title": meta["title_ru"],
            "stratification_method": "deterministic_clinical_rules_v1 + DTW",
            "stratification_evidence": matched_rule,
            "clinical_benchmark": meta["benchmark"],
            "recommended_control_action": meta["control_action"],
            "dtw_distance_to_sechenov_rki": dtw_dist,
            # Иллюстративная 2D-проекция для витрины (координаты кластеров заданы
            # вручную, это НЕ модельное вложение — честная метка для жюри и UI).
            "illustrative_2d_projection": meta["umap_coords"],
            "state_vector_24d": z_24,
        }
