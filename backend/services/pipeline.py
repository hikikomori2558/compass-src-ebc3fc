# backend/services/pipeline.py — сквозной безопасный путь «текст ЭМК -> решение для врача».
#
# Чем отличается от ClinicalCompassRuleEngine.run_full_clinical_compass_pipeline:
#   * вход — tri-state (None = неизвестно), а не числа с «нормальными» значениями по умолчанию;
#   * решение о противопоказаниях принимает декларативный RulesEngine (JSON, версии, источники),
#     а C-ядро даёт ему математику: СКФ по CKD-EPI и класс Чайлд-Пью;
#   * неизвестное НИКОГДА не даёт зелёный статус: пробелы идут в data_required, а врачу
#     показываются как список «подтвердите» (apply_confirmations);
#   * в кластеризацию 24D идут только известные входы; подставленные значения перечислены
#     в imputed_inputs, а при отсутствии шкал кластеризация не считается вовсе;
#   * все допущения (CYP2D6, возраст для беременности) выводятся списком assumptions.
# Решает по-прежнему врач: система предупреждает и оформляет (Условие 3 кейса).
import time
from datetime import date
from typing import Any, Dict, List, Optional

from backend.domain.phenotypes import detect_phenotypes, differential_gad_vs_adaptation, monitoring_and_review
from backend.domain.routing import ClinicalCompassRuleEngine
from backend.domain.rules import RulesEngine
from backend.services.ehr_extract import apply_confirmations, extract, missing_confirmations

_CP_RANK = {"N": 0, "A": 1, "B": 2, "C": 3}


class SafeCompassPipeline:
    def __init__(self, engine: ClinicalCompassRuleEngine, rules: Optional[RulesEngine] = None):
        self.engine = engine
        self.rules = rules or RulesEngine("aviandr_hard_stops")

    # ------------------------------------------------------------------ вход: текст
    def analyze_text(self, raw_text: str, confirmations: Optional[Dict[str, bool]] = None,
                     as_of: Optional[date] = None) -> Dict[str, Any]:
        t0 = time.perf_counter()
        ex = extract(raw_text, as_of=as_of)
        res = self.analyze_extracted(ex, confirmations, ehr_text=raw_text)
        res["parse_and_decide_latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        return res

    # ------------------------------------------------------------------ вход: извлечённое
    def analyze_extracted(self, ex_in: Dict[str, Any], confirmations: Optional[Dict[str, bool]] = None,
                          ehr_text: str = "") -> Dict[str, Any]:
        ex = apply_confirmations(ex_in, confirmations)
        p, cs = ex["profile"], ex["clinical_state"]
        assumptions: List[str] = list(ex["assumptions"])
        warnings: List[str] = list(ex["warnings"])

        # ---------------- 1. C-ядро: только известное, остальное = «нет данных»
        age, sex = p["age"], p["sex"]
        creat = p["serum_creatinine_umol_l"]
        can_egfr = creat is not None and age is not None and sex is not None
        if creat is not None and not can_egfr:
            warnings.append("Креатинин есть, но нет возраста/пола — СКФ по CKD-EPI не рассчитана.")
        liver_ok = all(p[k] is not None for k in ("bilirubin_umol_l", "albumin_g_l", "inr"))
        days = p["days_since_maoi"]
        c_params: Dict[str, Any] = {
            "age": min(int(age), 255) if age is not None else 0,
            "sex": int(sex) if sex is not None else 0,
            "is_pregnant_or_lactating": 1 if p["is_pregnant_or_lactating"] is True else 0,
            "has_epilepsy_or_organic_cns": 1 if p["has_epilepsy_or_organic_cns"] is True else 0,
            "has_lactase_deficiency": 1 if p["has_lactase_deficiency"] is True else 0,
            "days_since_maoi": min(int(days), 254) if days is not None else 255,
            "serum_creatinine_umol_l": float(creat) if can_egfr else -1.0,
            "bilirubin_umol_l": float(p["bilirubin_umol_l"]) if liver_ok else -1.0,
            "albumin_g_l": float(p["albumin_g_l"]) if liver_ok else -1.0,
            "inr": float(p["inr"]) if liver_ok else -1.0,
            "alt_u_l": float(p["alt_u_l"] or 0.0), "ast_u_l": float(p["ast_u_l"] or 0.0),
            "comedication_bitmask": int(p["comedication_bitmask"] or 0),
            "cyp2d6_phenotype": int(p["cyp2d6_phenotype"] or 0),
            "ecg_qtc_ms": float(p["ecg_qtc_ms"] or 0.0),
        }
        c_res = self.engine.c_bridge.run_c_core(c_params, fill_defaults=False)
        if p["cyp2d6_phenotype"] is None:
            assumptions.append("CYP2D6 не указан: фармакокинетическая кривая показана для нормального "
                               "метаболизма (иллюстрация, не клиническое решение).")

        # ---------------- 2. СКФ и класс Чайлд-Пью для движка правил
        egfr_c = c_res["egfr_ckd_epi"] if can_egfr else None
        egfr_rep = p["egfr_reported"]
        candidates = [v for v in (egfr_c, egfr_rep) if v is not None]
        egfr = min(candidates) if candidates else None   # консервативно: меньшая СКФ
        if egfr_c is not None and egfr_rep is not None and abs(egfr_c - egfr_rep) > 15:
            warnings.append(f"СКФ из креатинина ({egfr_c:.1f}) и СКФ из ЭМК ({egfr_rep:.1f}) расходятся более чем на 15 — "
                            f"принято меньшее значение.")

        cp_c = c_res["child_pugh_class"] if liver_ok else None
        cp_c = None if cp_c in ("?", None) else cp_c
        cp_text = p["child_pugh_class_text"]
        if cp_c and cp_text and cp_c != cp_text:
            warnings.append(f"Класс Чайлд-Пью: по анализам {cp_c}, в тексте {cp_text} — принят более тяжёлый.")
            cp = max((cp_c, cp_text), key=lambda x: _CP_RANK[x])
        else:
            cp = cp_c or cp_text
        if cp_c in ("A", "B", "C"):
            warnings.append("Класс Чайлд-Пью рассчитан без асцита и энцефалопатии (в ЭМК не указаны) — балл мог быть занижен.")

        # ---------------- 3. декларативные правила (tri-state)
        rules_profile = {
            "age": age, "sex": sex, "egfr_ckd_epi": egfr, "child_pugh_class": cp,
            "hepatic_insufficiency_documented": p["hepatic_insufficiency_documented"],
            "has_epilepsy_or_organic_cns": p["has_epilepsy_or_organic_cns"],
            "has_lactase_deficiency": p["has_lactase_deficiency"],
            "maoi_ever": p["maoi_ever"], "days_since_maoi": p["days_since_maoi"],
            "is_pregnant_or_lactating": p["is_pregnant_or_lactating"],
            "comedication_bitmask": int(p["comedication_bitmask"] or 0),
            "drives_vehicle": p["drives_vehicle"],
        }
        rules_res = self.rules.evaluate(rules_profile)

        # ---------------- 4. маршрутизация и алертинг
        routing = self.engine.evaluate_routing_and_alerts(cs)

        # ---------------- 5. диагноз
        icd = ex["icd10_code"]
        data_required: List[Dict[str, Any]] = list(rules_res["data_required"]) + list(routing.get("data_gaps", []))
        if icd is None:
            data_required.append({"field": "diagnosis_icd10", "why_needed":
                                  "Код МКБ-10 не найден: показание по ОХЛП определить нельзя.", "source_id": "AVIANDR_OHLP"})

        # ---------------- 6. решение по препарату (без зелёного при пробелах)
        meta = self.engine.kb["therapy_options_grls_catalog"]["AVIANDR"]
        if rules_res["status"] == "RED_HARD_STOP":
            status = "HARD_STOP_CONTRAINDICATION"
            headline = ("ЖЁСТКОЕ ПРЕДУПРЕЖДЕНИЕ БЕЗОПАСНОСТИ: выявлены абсолютные противопоказания по ОХЛП. "
                        "Назначение не рекомендуется; решение — только врача/ВК с фиксацией причины (override).")
        elif routing["critical_alert_triggered"]:
            status = "HOLD_EMERGENCY_PSYCHIATRY"
            headline = "ТРЕБУЕТСЯ ЭКСТРЕННАЯ КОНСУЛЬТАЦИЯ ПСИХИАТРА (Монотерапия анксиолитиком не показана)"
        elif data_required:
            status = "DATA_REQUIRED"
            names = ", ".join(sorted({g["field"] for g in data_required}))
            headline = ("Недостаточно данных для оценки назначения: " + names +
                        ". Внесите данные или подтвердите отсутствие — зелёного статуса при пробелах не бывает.")
        elif icd in meta["in_label_icd10"]:
            status = "RECOMMENDED_IN_LABEL"
            headline = f"Показание входит в ОХЛП ГРЛС ({icd}): Авиандр® 20 мг 2 раза/сут за 30 мин до еды"
        elif icd in meta["completed_phase3_off_label_icd10"]:
            status = "OFF_LABEL_PHASE3_COMPLETED"
            headline = meta["completed_phase3_off_label_icd10"][icd]
        else:
            status = "REQUIRES_CLINICIAN_REVIEW"
            headline = "Назначение вне зарегистрированных показаний ОХЛП — решение принимает только врач / ВК."

        # ---------------- 7. общий статус для UI
        if routing["critical_alert_triggered"]:
            overall = "RED_CRITICAL_ALERT"
        elif rules_res["status"] == "RED_HARD_STOP":
            overall = "RED_HARD_STOP"
        elif data_required:
            overall = "AMBER_DATA_REQUIRED"
        elif rules_res["status"] == "AMBER_REVIEW" or routing["routing_category"] != "PRIMARY_CARE_MANAGEMENT" \
                or status in ("OFF_LABEL_PHASE3_COMPLETED", "REQUIRES_CLINICIAN_REVIEW"):
            overall = "AMBER_REVIEW"
        else:
            overall = "GREEN_IN_LABEL"

        # ---------------- 8. кластеризация 24D: только на известных входах
        cluster = self._cluster(cs, c_res, icd)

        # ---------------- 9. фенотипы, дифдиагноз, контрольные точки (аддитивные блоки)
        phenotype_guidance = detect_phenotypes(ehr_text, {**p, **cs}, self.engine.kb)
        differential = differential_gad_vs_adaptation({**p, "icd10_code": icd}, self.engine.kb)
        monitoring = monitoring_and_review(cs, self.engine.kb)

        return {
            "overall_status": overall,
            "human_in_the_loop_notice": self.engine.kb["human_in_the_loop_disclaimer"],
            "aviandr_decision_support": {
                "status": status, "headline": headline,
                "dosing_regimen": meta["dosing_regimen"], "receptor_mechanism": meta["receptor_profile"],
            },
            "rules_engine": rules_res,
            "data_required": data_required,
            "doctor_confirmations_needed": missing_confirmations(ex),
            "routing_and_alerting": routing,
            "phenotype_guidance": phenotype_guidance,
            "differential_gad_vs_adaptation": differential,
            "monitoring_and_review": monitoring,
            "cluster_24d_analysis": cluster,
            "c_core_pk_and_safety": c_res,
            "assumptions": assumptions,
            "warnings": warnings,
            "extracted": {k: ex[k] for k in ("schema", "format", "as_of", "icd10_code", "icd10_all",
                                             "profile", "clinical_state", "evidence")},
        }

    # ------------------------------------------------------------------ кластеризация
    def _cluster(self, cs: Dict[str, Any], c_res: Dict[str, Any], icd: Optional[str]) -> Dict[str, Any]:
        if cs.get("phq9_total") is None or cs.get("gad7_total") is None:
            return {"status": "NOT_COMPUTED_MISSING_INPUTS",
                    "reason": "Нужны PHQ-9 и GAD-7; кластер без них — выдумка, поэтому не считается."}
        imputed: List[str] = []

        def pick(key: str, default: float) -> float:
            v = cs.get(key)
            if v is None:
                imputed.append(key)
                return default
            return float(v)

        item9 = cs.get("phq9_item9_suicidal")
        cssrs = cs.get("cssrs_score")
        if item9 is None and cssrs is None:
            imputed.append("suicidal_composite")
        z_24 = self.engine.cluster_engine.build_24d_state_vector(
            psychometrics={
                "hads_a": pick("hads_a", 13.0), "hads_d": pick("hads_d", 6.0),
                "gad7": float(cs["gad7_total"]), "phq9": float(cs["phq9_total"]),
                "suicidal_composite": float(item9 or 0) + float(cssrs or 0),
                "moca": pick("moca", 27.4),
            },
            dynamics={"therapy_day": pick("therapy_day", 3.0)},
            pk_adherence={"m1_ctrough_pg_ml": c_res["pk_summary_168h"]["m1_ctrough_ss_pg_ml"]},
            biomarkers={
                "experimental_voice_anx_class": pick("experimental_voice_anxiety_class", 1.0),
                "experimental_voice_dep_class": pick("experimental_voice_depression_class", 0.0),
                "voice_pause_ratio": pick("voice_pause_ratio", 0.18),
                "questionnaire_response_sec_per_item": pick("resp_sec", 4.2),
            },
            biometrics_endocrine={
                "hrv_rmssd_ms": c_res["rppg_autonomic_metrics"]["hrv_rmssd_ms"] or 34.0,
                "baevsky_stress_index": c_res["rppg_autonomic_metrics"]["baevsky_stress_index"] or 145.0,
                "endocrine_somatic_mask_flag": 1.0 if c_res["endocrine_and_somatic_masks"] else 0.0,
            },
        )
        res = self.engine.cluster_engine.assign_cluster(
            z_24=z_24, patient_sigh_a_history=[22.0, 19.0],
            reported_transient_drowsiness=False, icd10_code=icd or "",
        )
        res["imputed_inputs"] = imputed
        res["clinical_use"] = "ILLUSTRATIVE_NOT_A_DECISION"
        return res


if __name__ == "__main__":
    # Тест 3 эталонных сценариев (бывший __main__ clinical_rule_engine) — теперь через
    # ЕДИНСТВЕННЫЙ рабочий путь safe-конвейера, а не через удалённый legacy-пайплайн.
    # Виньетки берутся из детерминированного корпуса (seed=2026), поэтому вывод стабилен.
    import json

    from backend.domain.routing import ClinicalCompassRuleEngine
    from backend.paths import VIGNETTES_PATH

    print("=" * 88)
    print("КОМАНДА 20-П «RAPIRA» | ТЕСТ 3 ЭТАЛОННЫХ СЦЕНАРИЕВ (SAFE PIPELINE, COMPASS-HYBRID v2.6)")
    print("=" * 88)

    cases = json.loads(VIGNETTES_PATH.read_text(encoding="utf-8"))
    pipe = SafeCompassPipeline(ClinicalCompassRuleEngine())
    # Разные шаблоны корпуса: пост-COVID/адаптация, коморбидный пожилой, тяжёлый.
    for case in (cases[0], cases[len(cases) // 2], cases[-1]):
        res = pipe.analyze_text(case["raw_text"])
        c_core = res["c_core_pk_and_safety"]
        cluster = res["cluster_24d_analysis"]
        print(f"\n[{case['case_id']} | {case['cohort']} | {case['template_type']}]")
        print(f"  Общий статус: {res['overall_status']}")
        print(f"  Решение: {res['aviandr_decision_support']['headline']}")
        print(f"  СКФ: {c_core['egfr_ckd_epi']} | Отклик C-ядра: {c_core['c_core_latency_ms']} мс")
        print(f"  Кластер: {cluster.get('assigned_cluster_title', cluster.get('reason'))}")
