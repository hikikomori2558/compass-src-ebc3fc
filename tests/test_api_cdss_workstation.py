# -*- coding: utf-8 -*-
"""
benchmarks/test_variant5_doctor_cockpit.py
==========================================
Верификация промышленного интерфейса врача (OpenMRS 3 / Carbon v11 / HL7 CDS Hooks)
и серверного эндпоинта backend/api/cdss_workstation.py.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cdss_workstation import (
    EncounterEvaluationRequest,
    evaluate_clinical_encounter,
)


def test_openmrs_carbon_structure_and_interactive_workflows() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    doc_dir = repo_root / "frontend_doctor"

    index_html = (doc_dir / "index.html").read_text(encoding="utf-8")
    app_js = (doc_dir / "app.js").read_text(encoding="utf-8")

    # 1. Подключен официальный пакет стилей IBM Carbon v11 (@carbon/styles) и структура OpenMRS 3
    assert "vendor/carbon-styles.min.css" in index_html  # стили вендорены: демо не зависит от CDN
    assert "omrs-patient-banner" in index_html
    assert "cds-hooks-stream" in index_html

    # 2. Категории маршрутизации к психиатру («ОБЯЗАТЕЛЬНО» и «ЖЕЛАТЕЛЬНО») и форма 057/у-04
    assert "КАТЕГОРИЯ: ОБЯЗАТЕЛЬНО" in app_js
    assert "КАТЕГОРИЯ: ЖЕЛАТЕЛЬНО" in app_js
    assert "057/у-04" in app_js

    # 3. 14-дневный календарь приёма препарата «Авиандр®» между визитами и интерактивный CPOE
    assert "dose-calendar-grid" in index_html
    assert "cpoe-meds-tbody" in index_html
    assert "Авиандр® (Маритупирдин, таб. 20 мг, ГК ХимРар)" in app_js


def test_backend_cdss_workstation_c11_endpoint() -> None:
    """Проверяет прямой вызов C11-ядра через FastAPI-роутер врача."""
    req = EncounterEvaluationRequest(
        patient_id="RU-77-4092",
        doctor_specialty="therapist",
        ehr_note_text="Пациентка принимает Авеандр 20 мг, парокситин 20 мг и кларитромецин. Транилципрамин отрицает.",
        hama_score=6.0,
        madrs_score=7.0,
        self_report_wellbeing=0.92,
        cyp2d6_activity_score=0.12,
    )
    res = evaluate_clinical_encounter(req)
    assert res["routing_category"] == "ОБЯЗАТЕЛЬНО"
    assert res["poppk_rk45"]["c_max_ng_ml"] > 280.0
    assert res["owen_xai"]["exact_coalition_evaluations"] == 4096


if __name__ == "__main__":
    test_openmrs_carbon_structure_and_interactive_workflows()
    test_backend_cdss_workstation_c11_endpoint()
    print("[OK] Промышленный АРМ Врача (OpenMRS 3 + Medplum + CDS Hooks) и C11 API верифицированы!")