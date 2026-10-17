# bio_paths.py — Единая карта путей биоконтура COMPASS-HYBRID v2.6 (команда 20-П «Rapira»)
# Все модули биоконтура резолвят файлы через этот модуль, поэтому скрипты можно
# запускать из любой директории: python3 backend/clinical_rule_engine.py,
# python3 benchmarks/ehr_nlp_benchmark.py и т.д.
import sys
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

# Плоские импорты биоконтура: backend <-> benchmarks работают при любом способе запуска
for _p in (str(REPO_ROOT / "backend"), str(BENCHMARKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
