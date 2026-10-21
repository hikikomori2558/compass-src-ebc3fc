# backend/paths.py — Единая карта путей контура COMPASS-HYBRID v2.6 (команда 20-П «Rapira»).
# Все модули резолвят файлы через этот модуль. Запуск — из корня репозитория:
# python3 -m backend.main, python3 -m benchmarks.ehr_nlp_benchmark и т.д.
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Уровень I: нативное ядро C11 (собирается scripts/build_c_core.py -> c_core/libcompass_core.so)
C_CORE_DIR = REPO_ROOT / "c_core"
CORE_LIB = C_CORE_DIR / "libcompass_core.so"
CORE_SRC = C_CORE_DIR / "compass_core.c"
CORE_HEADER = C_CORE_DIR / "compass_core.h"

# Клинические данные (детерминированные JSON-реестры и синтетический корпус)
CLINICAL_DATA_DIR = REPO_ROOT / "clinical_data"
KB_PATH = CLINICAL_DATA_DIR / "clinical_knowledge_base.json"
VIGNETTES_PATH = CLINICAL_DATA_DIR / "synthetic_100_ehr_vignettes.json"

# Апробация и научные графики
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
BIO_PLOTS_DIR = BENCHMARKS_DIR / "plots"
PROTOCOL_PATH = BENCHMARKS_DIR / "PROTOCOL_APROBATION_2026.md"

