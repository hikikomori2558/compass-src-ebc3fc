# scripts/cache_kintsugi_offline.py
"""
Генератор локального манифеста калибровочных порогов голосового радара KintsugiHealth/dam.
ВАЖНО (Аудит доступности 2026): Внешний датасет KintsugiHealth/dam-dataset на Hugging Face
закрыт/удален (возвращает HTTP 401 Unauthorized). Поэтому числовые пороги акустического DSP
и сопоставление с классами PHQ-9 / GAD-7 явно маркируются как «РАБОЧАЯ ГИПОТЕЗА» (Working Hypothesis),
требующая проспективной калибровки на русскоязычной клинической выборке.
"""
import json
from pathlib import Path
from typing import Dict, Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CLINICAL_DATA_DIR = REPO_ROOT / "clinical_data"


def build_offline_calibration_manifest() -> Dict[str, Any]:
    return {
        "manifest_version": "2026.10.v2.6-honest-audit",
        "calibration_status": "HYPOTHESIS_REQUIRES_RU_VALIDATION",
        "dataset_availability_audit": {
            "hf_model_repo": "KintsugiHealth/dam (веса модели доступны по лицензии Apache 2.0)",
            "hf_dataset_repo": "KintsugiHealth/dam-dataset — УДАЛЕН / ЗАКРЫТ В СЕТИ (HTTP 401 Unauthorized)",
            "methodological_note": (
                "Поскольку оригинальный калибровочный датасет KintsugiHealth/dam-dataset недоступен (401), "
                "приведенные ниже пороговые квантили акустических признаков (Pause Ratio, Shimmer, Jitter) "
                "являются расчетной рабочей гипотезой на основе статьи arXiv:2605.09908 и требуют "
                "обязательной калибровки на русскоязычной выборке пациентов перед клиническим применением."
            ),
        },
        "depression_phq9_hypothesis_thresholds": {
            "0": {
                "class_id": 0,
                "label_ru": "Отсутствие выраженных акустических маркеров депрессии (гипотеза: PHQ-9 <= 9)",
                "c_dsp_hypothesis_bounds": {"pause_ratio_max": 0.27, "shimmer_local_pct_max": 3.9},
            },
            "1": {
                "class_id": 1,
                "label_ru": "Умеренные акустические маркеры заторможенности (гипотеза: PHQ-9 10–14)",
                "c_dsp_hypothesis_bounds": {"pause_ratio_range": [0.28, 0.39], "shimmer_local_pct_range": [4.0, 5.9]},
            },
            "2": {
                "class_id": 2,
                "label_ru": "Выраженные акустические маркеры психомоторной заторможенности (гипотеза: PHQ-9 >= 15)",
                "c_dsp_hypothesis_bounds": {"pause_ratio_min": 0.40, "shimmer_local_pct_min": 6.0},
            },
        },
        "anxiety_gad7_hypothesis_thresholds": {
            "0": {"class_id": 0, "label_ru": "Минимальный тремор голоса (гипотеза: GAD-7 <= 4)", "jitter_max": 0.89},
            "1": {"class_id": 1, "label_ru": "Легкий тремор голоса (гипотеза: GAD-7 5–9)", "jitter_max": 1.39},
            "2": {"class_id": 2, "label_ru": "Умеренный тремор голоса (гипотеза: GAD-7 10–14)", "jitter_max": 2.19},
            "3": {"class_id": 3, "label_ru": "Выраженный тремор голоса (гипотеза: GAD-7 >= 15)", "jitter_min": 2.20},
        },
        "cluster_k4_dissimulation_hypothesis": {
            "status": "WORKING_HYPOTHESIS",
            "rule_description": (
                "Флаг возможной диссимуляции (Кластер К4) срабатывает как вспомогательный сигнал для врача, "
                "если пациент заполняет опросник PHQ-9 с низким баллом (< 10) и высокой скоростью (< 1.5 с/вопрос), "
                "но акустический DSP фиксирует долю пауз >= 40%. Не является самостоятельным диагнозом."
            ),
        },
    }


def main() -> None:
    CLINICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLINICAL_DATA_DIR / "kintsugi_dam_offline_calibration.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(build_offline_calibration_manifest(), f, ensure_ascii=False, indent=2)
    print(f"[OK] Манифест калибровки Kintsugi (статус: рабочая гипотеза, dam-dataset 401) сохранен: {out_path}")


if __name__ == "__main__":
    main()
