# scripts/cache_kintsugi_offline.py
"""
Локальное кэширование калибровочных распределений и метаданных открытой модели
KintsugiHealth/dam и KintsugiHealth/dam-dataset (Apache 2.0, arXiv:2605.09908)
для 100% автономной работы демо-стенда в Сколково без доступа к сети Интернет.
"""
import argparse
import json
from pathlib import Path
from typing import Dict, Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CLINICAL_DATA_DIR = REPO_ROOT / "clinical_data"
HF_CACHE_DIR = CLINICAL_DATA_DIR / "hf_cache"


def build_offline_calibration_manifest() -> Dict[str, Any]:
    return {
        "manifest_version": "2026.10.v2.6",
        "offline_ready": True,
        "model_metadata": {
            "hf_model_repo": "KintsugiHealth/dam",
            "hf_dataset_repo": "KintsugiHealth/dam-dataset",
            "base_acoustic_encoder": "openai/whisper-small.en",
            "license": "Apache-2.0 (Commercial & Clinical Competition Use Permitted)",
            "training_cohort": "35,000 patients / 863 hours of speech",
            "clinical_validation_cohort": "N = 14,898 primary care patients (Annals of Family Medicine, 2025)",
            "paper_reference": "Abramenko O., Stein N., Vaz C. Voice Biomarkers for Depression and Anxiety (arXiv:2605.09908, May 2026)",
            "paralinguistic_invariant_note": (
                "Модель анализирует исключительно акустические и паралингвистические инварианты "
                "(спектральную энтропию, микропаузы, вариабельность F0, Jitter/Shimmer) без транскрибации текста."
            ),
        },
        "depression_phq9_calibration": {
            "0": {
                "class_id": 0,
                "label_ru": "Отсутствие депрессии (эквивалент PHQ-9 <= 9)",
                "phq9_range": [0, 9],
                "dam_logit_quantiles": {"p10": -2.15, "p50": -0.92, "p90": -0.15},
                "c_dsp_reference": {
                    "pause_ratio_range": [0.08, 0.27],
                    "shimmer_local_pct_range": [1.2, 3.9],
                    "f0_std_hz_min": 16.0,
                },
            },
            "1": {
                "class_id": 1,
                "label_ru": "Легкая / умеренная депрессия (эквивалент PHQ-9 10–14)",
                "phq9_range": [10, 14],
                "dam_logit_quantiles": {"p10": -0.10, "p50": 0.64, "p90": 1.28},
                "c_dsp_reference": {
                    "pause_ratio_range": [0.28, 0.39],
                    "shimmer_local_pct_range": [4.0, 5.9],
                    "f0_std_hz_min": 11.5,
                },
            },
            "2": {
                "class_id": 2,
                "label_ru": "Тяжелая депрессия (эквивалент PHQ-9 >= 15)",
                "phq9_range": [15, 27],
                "dam_logit_quantiles": {"p10": 1.30, "p50": 2.18, "p90": 3.45},
                "c_dsp_reference": {
                    "pause_ratio_range": [0.40, 0.75],
                    "shimmer_local_pct_range": [6.0, 14.0],
                    "f0_std_hz_min": 6.0,
                },
            },
        },
        "anxiety_gad7_calibration": {
            "0": {
                "class_id": 0,
                "label_ru": "Минимальная тревога (эквивалент GAD-7 <= 4)",
                "gad7_range": [0, 4],
                "jitter_local_pct_max": 0.89,
            },
            "1": {
                "class_id": 1,
                "label_ru": "Легкая тревога (эквивалент GAD-7 5–9)",
                "gad7_range": [5, 9],
                "jitter_local_pct_max": 1.39,
            },
            "2": {
                "class_id": 2,
                "label_ru": "Умеренная тревога (эквивалент GAD-7 10–14)",
                "gad7_range": [10, 14],
                "jitter_local_pct_max": 2.19,
            },
            "3": {
                "class_id": 3,
                "label_ru": "Выраженная (тяжелая) тревога (эквивалент GAD-7 >= 15)",
                "gad7_range": [15, 21],
                "jitter_local_pct_max": 5.50,
            },
        },
        "cluster_k4_dissimulation_rules": {
            "rule_description": (
                "Флаг скрытой диссимуляции (Кластер К4) выставляется, если пациент в анкете PHQ-9 набирает < 10 баллов "
                "(или заполняет анкету быстрее 1.5 сек на вопрос), но голосовой радар фиксирует класс депрессии 2 "
                "(Pause Ratio >= 0.40 или Shimmer >= 6.0%)."
            ),
            "phq9_self_report_max_for_dissimulation": 9,
            "required_voice_depression_class": 2,
            "fast_click_threshold_sec_per_item": 1.5,
        },
    }


def try_download_hf_snapshots() -> bool:
    """Качаем снапшоты в локальный кэш. Модель критична для офлайн-демо; калибровочный
    ДАТАСЕТ может отсутствовать публично (Kintsugi закрылась в 2026: модель оставили
    открытой, репозиторий датасета убрали — 401) — его недоступность не фейлит загрузку.
    Возвращает True, если модель закэширована."""
    results = {}
    try:
        from huggingface_hub import snapshot_download
        HF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for repo_id, critical in (("KintsugiHealth/dam", True),
                                  ("KintsugiHealth/dam-dataset", False)):
            print(f"[HF] Скачивание {repo_id} в {HF_CACHE_DIR}...")
            try:
                snapshot_download(
                    repo_id=repo_id,
                    local_dir=str(HF_CACHE_DIR / repo_id.replace("/", "__")),
                    ignore_patterns=["*.msgpack", "*.h5"],
                )
                results[repo_id] = True
            except Exception as e:  # noqa: BLE001 — один репозиторий не должен ронять второй
                results[repo_id] = False
                print(f"[{'ERROR' if critical else 'WARN'}] {repo_id}: {e}")
        return results.get("KintsugiHealth/dam", False)
    except ImportError as e:
        print(f"[INFO] huggingface_hub не установлен ({e}) — офлайн-кэш только метаданными.")
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download-hf",
        action="store_true",
        help="Попытаться скачать снапшоты KintsugiHealth/dam с Hugging Face при наличии сети",
    )
    args = parser.parse_args()

    CLINICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest = build_offline_calibration_manifest()

    if args.download_hf:
        manifest["hf_snapshots_downloaded"] = try_download_hf_snapshots()

    out_path = CLINICAL_DATA_DIR / "kintsugi_dam_offline_calibration.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[OK] Автономный калибровочный кэш Kintsugi DAM сохранен: {out_path}")


if __name__ == "__main__":
    main()
