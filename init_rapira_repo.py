#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init_rapira_repo.py — Автоматический инициализатор командного монорепозитория
Проект: COMPASS-HYBRID v2.6 (Команда 20-П «Rapira»)
"""

import os
import subprocess
from pathlib import Path

CWD = Path.cwd()
ROOT = CWD if CWD.name == "rapira-clinical-compass" else CWD / "rapira-clinical-compass"

DIRS = [
    "c_core",
    "micro_nets",
    "backend",
    "frontend_doctor",
    "frontend_patient",
    "clinical_data",
    "benchmarks/plots",
    "scripts",
    "docs",
    ".vscode",
]

GITIGNORE = """# Python & окружение
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
env/
.pytest_cache/

# Скомпилированные бинарники C-Core (собираются локально через scripts/build_c_core.py)
*.o
*.obj
*.so
*.dylib
*.dll
c_core/build/

# Тяжелые веса внешних моделей с Hugging Face (кэшируются локально)
models_cache/
*.bin
*.safetensors

# Локальные БД и системные файлы
*.db
*.sqlite3
postgres_data/
redis_data/
.DS_Store
Thumbs.db
.env
"""

GITATTRIBUTES = """* text=auto eol=lf
*.c text eol=lf
*.h text eol=lf
*.py text eol=lf
*.sh text eol=lf
*.md text eol=lf
*.json text eol=lf
*.png binary
*.jpg binary
*.jpeg binary
*.pdf binary
*.onnx binary
"""

COMPASS_CORE_H = """#ifndef COMPASS_CORE_H
#define COMPASS_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Единый бинарный контракт нативного ядра COMPASS-HYBRID v2.6 (libcompass_core.so)
 * Связка C11 <-> Python через cffi (ABI/API без копирования памяти).
 */

typedef struct {
    uint8_t age;                           /* Возраст (< 18 лет -> безусловная блокировка) */
    uint8_t sex;                           /* Пол: 0 — женский, 1 — мужской (для СКФ CKD-EPI 2021) */
    uint8_t is_pregnant_or_lactating;      /* Беременность или период лактации (0 или 1) */
    uint8_t has_epilepsy_or_organic_cns;   /* Эпилепсия или органическое поражение ЦНС (0 или 1) */
    uint8_t has_lactase_deficiency;        /* Дефицит лактазы / непереносимость галактозы / LCT C/C (0 или 1) */
    uint8_t days_since_maoi;               /* Полных суток с момента отмены ИМАО (<= 14 -> блокировка) */
    float serum_creatinine_umol_l;         /* Креатинин сыворотки крови (мкмоль/л) */
    float alt_u_l;                         /* АЛТ (Ед/л) */
    float ast_u_l;                         /* АСТ (Ед/л) */
    float bilirubin_umol_l;                /* Билирубин общий (мкмоль/л) */
    float albumin_g_l;                     /* Альбумин (г/л) */
    float inr;                             /* МНО (для расчёта Чайлд-Пью) */
    uint8_t cyp2d6_phenotype;              /* 0 = NM, 1 = IM, 2 = PM (медленный), 3 = UM */
    uint8_t cyp2c19_phenotype;             /* Панель фармакогенетики CYP2C19 */
    uint8_t cyp1a2_inducer_status;         /* Статус индукции/ингибирования CYP1A2 */
    uint8_t slc6a4_htr2a_ssri_intolerance; /* Маркеры непереносимости СИОЗС (SLC6A4 / HTR2A) */
    uint32_t comedication_bitmask;         /* Битовая маска сопутствующих препаратов ГРЛС */
    float tsh_mIU_l;                       /* ТТГ (мМЕ/л) — исключение тиреотоксикоза/гипотиреоза */
    float free_t4_pmol_l;                  /* Свободный Т4 (пмоль/л) */
    float cortisol_morning_nmol_l;         /* Утренний кортизол (нмоль/л) */
    float ferritin_ug_l;                   /* Ферритин (мкг/л, порог < 30) */
    float hemoglobin_g_l;                  /* Гемоглобин (г/л, порог < 120) */
    float cpk_u_l;                         /* КФК (Ед/л) */
    float ecg_qtc_ms;                      /* Интервал QTc на ЭКГ (мс) */
} PatientClinicalInput;

typedef struct {
    uint32_t hard_stop_flags;              /* Битовая маска 8 абсолютных противопоказаний ОХЛП Авиандр® */
    uint32_t ddi_warning_flags;            /* Флаги опасных межлекарственных взаимодействий */
    uint32_t ddi_verified_safe;            /* Безопасные комбинации (метопролол, дулоксетин, ципрофлоксацин, флуконазол) */
    float egfr_ckd_epi;                    /* Расчётная СКФ (мл/мин/1.73 м2, порог < 60) */
    uint8_t child_pugh_score;              /* Сумма баллов Чайлд-Пью (5..15) и класс A/B/C */
    float pk_curve_parent[168];            /* Почасовая концентрация маритупирдина (пг/мл) на 168 ч (7 суток) */
    float pk_curve_m1[168];                /* Почасовая концентрация активного метаболита М1 (пг/мл) на 168 ч */
    float transient_sedation_prob_day1_4;  /* Вероятность преходящей сонливости/головокружения в 1-4 дни */
    uint32_t endocrine_somatic_mask_flags; /* Флаги эндокринных и соматических масок тревоги и запаса QTc */
    float hrv_rmssd_ms;                    /* Вариабельность сердечного ритма RMSSD (мс) */
    float baevsky_stress_index;            /* Индекс вегетативного напряжения Баевского */
} CompassCoreOutput;

int compass_evaluate_patient(const PatientClinicalInput* input, CompassCoreOutput* output);
int compass_scrub_pii(const char* raw_text, char* scrubbed_buffer, int buffer_len);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_CORE_H */
"""

C_CORE_README = """# Нативное ядро C-Core (libcompass_core.so)

## Памятка для Михаила (Разработчик №2)
Закидывай в эту папку (c_core/) свои готовые модули и локальные тесты:
- pk_cyp2d6_solver.c — решатель РК4 (168 ч, маритупирдин + метаболит М1, фенотипы CYP2D6 Normal vs Poor Metabolizer *4/*4).
- ohpl_hardstops.c — 8 абсолютных противопоказаний ОХЛП «Авиандр®», СКФ по CKD-EPI, Чайлд-Пью, матрица взаимодействий DDI.
- voice_dsp.c — расчёт акустических параметров голоса (F0, Jitter, Shimmer, Pause Ratio).

## Памятка для Всеволода (Разработчик №1)
- pii_scrubber.c — потоковое обезличивание ПДн в оперативной памяти (152-ФЗ УЗ-1).
- simd_lexer.c — быстрый лексер кодов МКБ-10 и токенов ГРЛС (алгоритм Ахо — Корасик).

Для компиляции на любой ОС нажмите в VS Code Cmd+Shift+B (macOS) или Ctrl+Shift+B (Windows/Linux), либо выполните в терминале:
    python3 scripts/build_c_core.py
"""

PLOTS_README = """# Графики и результаты локальных тестов (benchmarks/plots/)

Михаил, закидывай сюда сгенерированные графики:
1. pk_profile_aviandr_168h_rk4.jpg — Фармакокинетический профиль Авиандр® (20 мг 2 р/сут за 30 мин до еды, 168 ч RK4 C-Core, CYP2D6 Normal vs Poor Metabolizer).
2. therapy_monitoring_36w_corridor.jpg — Мониторинг динамики терапии Авиандр® относительно 36-недельного коридора РКИ Сеченовского университета (N=98, SIGH-A).
3. umap_clusters_24d_gmm_dtw.jpg — Кластерный анализ 24-мерных векторов состояния пациентов (z_t in R^24, GMM + DTW, кластеры К1–К5).
"""

MICRO_NETS_STARTER = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
\"\"\"
Контур II (Control Plane): 3 легковесные микросети управления (Всеволод — Разработчик №1)
1. RouterNet-1.1M (Gated Residual MLP со Sparse-маской, ~1.12M параметров, отклик ~0.35 мс)
2. DissimulationDropoutNet-1.6M (Dilated 1D-TCN по скользящему окну 7x24, ~1.58M параметров, отклик ~0.62 мс)
3. ClusterVerifyNet-0.7M (Denoising Autoencoder + Классификатор переходов К1-К5, ~0.68M параметров, отклик ~0.25 мс)
\"\"\"

# Сюда Всеволод коммитит архитектуру PyTorch и скрипт экспорта в ONNX Runtime.
"""

BUILD_C_CORE_PY = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
\"\"\"
Кроссплатформенный сборщик нативного ядра libcompass_core.so
Автоматически определяет macOS (Apple Silicon M-series) / Linux / Windows.
\"\"\"

import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C_DIR = ROOT / "c_core"
OUT_LIB = C_DIR / "libcompass_core.so"

def main():
    c_files = sorted(C_DIR.glob("*.c"))
    if not c_files:
        print("[!] В папке c_core/ пока нет .c файлов (ожидаем заливку C-Core от Михаила).")
        sys.exit(0)

    arch = platform.machine().lower()
    sys_name = platform.system()

    cmd = ["gcc", "-O3", "-shared", "-fPIC"]
    if "x86_64" in arch or "amd64" in arch:
        cmd.append("-mavx2")
    elif "arm" in arch or "aarch64" in arch:
        cmd.append("-mcpu=native")

    cmd.extend([str(f) for f in c_files])
    cmd.extend(["-o", str(OUT_LIB), "-lm"])

    print(f"[*] Компиляция C-Core ({sys_name} / {arch}):")
    print("    " + " ".join(cmd))
    t0 = time.perf_counter()
    res = subprocess.run(cmd, capture_output=True, text=True)
    dt = (time.perf_counter() - t0) * 1000.0

    if res.returncode != 0:
        print("[ERROR] Ошибка компиляции:\\n", res.stderr)
        sys.exit(res.returncode)

    print(f"[OK] Собрана библиотека {OUT_LIB.name} за {dt:.1f} мс.")

if __name__ == "__main__":
    main()
"""

SMOKE_TEST_PY = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
\"\"\"
Смоук-тест и бенчмарк C-Core (Метрика №1: целевой отклик < 0.15 мс)
\"\"\"

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_PATH = ROOT / "c_core" / "libcompass_core.so"

if __name__ == "__main__":
    if not LIB_PATH.exists():
        print(f"[!] Библиотека {LIB_PATH.name} еще не собрана.")
        print("    После того как Михаил зальет .c файлы, запусти: python3 scripts/build_c_core.py")
        raise SystemExit(0)
    print(f"[OK] Библиотека {LIB_PATH} найдена и готова к вызовам через cffi.")
"""

SYNC_GIT_PY = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
\"\"\"
Безопасная синхронизация репозитория (git pull --rebase origin main)
\"\"\"

import subprocess
import sys

def main():
    print("=== Синхронизация rapira-clinical-compass с GitHub ===")
    subprocess.run(["git", "status", "-s"])
    res = subprocess.run(["git", "pull", "--rebase", "origin", "main"])
    if res.returncode != 0:
        print("[!] Сначала закоммить свои локальные изменения в GitHub Desktop, затем повтори Pull.")
        sys.exit(res.returncode)
    print("[OK] Код успешно обновлен!")

if __name__ == "__main__":
    main()
"""

VSCODE_TASKS = """{
    "version": "2.0.0",
    "tasks": [
        {
            "label": "1. Собрать C-Core (libcompass_core.so)",
            "type": "shell",
            "command": "python3 scripts/build_c_core.py",
            "group": {
                "kind": "build",
                "isDefault": true
            },
            "problemMatcher": ["$gcc"],
            "presentation": {
                "reveal": "always",
                "panel": "shared"
            }
        },
        {
            "label": "2. Синхронизировать с GitHub (git pull --rebase)",
            "type": "shell",
            "command": "python3 scripts/sync_git.py",
            "presentation": {
                "reveal": "always",
                "panel": "shared"
            }
        },
        {
            "label": "3. Проверить C-Core (Smoke Test)",
            "type": "shell",
            "command": "python3 scripts/smoke_test_c_core.py",
            "presentation": {
                "reveal": "always",
                "panel": "shared"
            }
        }
    ]
}
"""

VSCODE_SETTINGS = """{
    "files.encoding": "utf8",
    "files.eol": "\\n",
    "editor.formatOnSave": true,
    "C_Cpp.default.cStandard": "c11",
    "C_Cpp.default.includePath": [
        "${workspaceFolder}/c_core"
    ]
}
"""

README_MD = """# COMPASS-HYBRID v2.6 — Команда 20-П «Rapira» (rapira-clinical-compass)

Клиническая система поддержки принятия врачебных решений (CDSS) и цифрового сопровождения терапии препаратом **«Авиандр®» (маритупирдин)**.

---

## 1. Пошаговая инструкция для нас двоих (GitHub Desktop + VS Code)

### Шаг А. Что делает Всеволод (Инициализация)
1. Запускает `python3 init_rapira_repo.py` — создаётся структура папок с уже инициализированным Git и первым коммитом.
2. Открывает **GitHub Desktop** -> **File -> Add Local Repository...** -> указывает папку `rapira-clinical-compass`.
3. Нажимает синюю кнопку **Publish repository** вверху справа (галочка **Keep this code private** включена!).
4. На странице репозитория на GitHub открывает **Settings -> Collaborators -> Add people** и добавляет Михаила.

### Шаг Б. Что делает Михаил (Заливка готового C-Core, тестов и графиков)
1. Принимает инвайт на GitHub.
2. В **GitHub Desktop** нажимает **File -> Clone Repository...** -> выбирает `rapira-clinical-compass` -> **Clone**.
3. Копирует свои готовые `.c` файлы и локальные тесты в папку `c_core/`, а готовые графики (PK-профиль 168 ч RK4, коридор 36 недель РКИ Сеченовки, UMAP кластеры К1–К5) — в папку `benchmarks/plots/`.
4. В **GitHub Desktop** слева внизу пишет в поле Summary: `feat(c_core): заливка готового C-ядра, тестов и графиков PK/UMAP` -> нажимает **Commit to main** -> вверху справа нажимает **Push origin**.

### Шаг В. Как мы работаем дальше параллельно без конфликтов
Мы работаем в разных папках, поэтому **Git не выдаст конфликт слияния**, если соблюдать 3 простых правила:
1. **Сел за VS Code — сначала нажми Fetch origin / Pull origin в GitHub Desktop** (чтобы забрать свежие коммиты напарника).
2. **Всеволод** коммитит в:
   - `/micro_nets/` — три микросети управления (`RouterNet-1.1M`, `DissimulationDropoutNet-1.6M`, `ClusterVerifyNet-0.7M`).
   - `/frontend_doctor/` и `/frontend_patient/` — ЭМК-сайдбар врача (420px), дашборд и PWA/TG пациента.
   - `/backend/` — FastAPI, БД (12 таблиц), WebSocket-алертинг, JSONLogic, Pydantic-парсер ЭМК.
   - `/c_core/pii_scrubber.c` и `/c_core/simd_lexer.c`.
3. **Михаил** коммитит в:
   - `/c_core/pk_cyp2d6_solver.c`, `/c_core/ohpl_hardstops.c`, `/c_core/voice_dsp.c`.
   - `/clinical_data/` — JSON-правила ОХЛП «Авиандра», реестр 7 шкал, кластеризацию `trajectory_clustering.py` (GMM + DTW), интеграцию `KintsugiHealth/dam` и генератор 100 синтетических историй болезни.
   - `/benchmarks/plots/` — научные графики и метрики валидации.
4. **Сделал законченный кусок (раз в 1–2 часа)** -> в GitHub Desktop написал короткий Summary -> **Commit to main** -> **Push origin**.

---

## 2. Горячие клавиши в VS Code
- `Cmd+Shift+B` (macOS) / `Ctrl+Shift+B` (Windows/Linux) — скомпилировать `c_core/*.c` в `libcompass_core.so`.
- `Cmd+Shift+P` / `Ctrl+Shift+P` -> `Tasks: Run Task` -> **2. Синхронизировать с GitHub** — быстрый `git pull --rebase`.
"""

def write_file(rel_path: str, content: str, executable: bool = False):
    p = ROOT / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    if executable:
        os.chmod(p, 0o755)

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        dir_path = ROOT / d
        dir_path.mkdir(parents=True, exist_ok=True)
        gitkeep = dir_path / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")

    write_file(".gitignore", GITIGNORE)
    write_file(".gitattributes", GITATTRIBUTES)
    write_file("c_core/compass_core.h", COMPASS_CORE_H)
    write_file("c_core/README_C_CORE.md", C_CORE_README)
    write_file("benchmarks/plots/README_PLOTS.md", PLOTS_README)
    write_file("micro_nets/micro_nets_arch.py", MICRO_NETS_STARTER)
    write_file("scripts/build_c_core.py", BUILD_C_CORE_PY, executable=True)
    write_file("scripts/smoke_test_c_core.py", SMOKE_TEST_PY, executable=True)
    write_file("scripts/sync_git.py", SYNC_GIT_PY, executable=True)
    write_file(".vscode/tasks.json", VSCODE_TASKS)
    write_file(".vscode/settings.json", VSCODE_SETTINGS)
    write_file("README.md", README_MD)

    if not (ROOT / ".git").exists():
        subprocess.run(["git", "init"], cwd=ROOT, check=False)
        subprocess.run(["git", "branch", "-M", "main"], cwd=ROOT, check=False)

    subprocess.run(["git", "add", "."], cwd=ROOT, check=False)
    subprocess.run(
        ["git", "commit", "-m", "chore: init rapira-clinical-compass monorepo structure"],
        cwd=ROOT,
        check=False,
    )

    print("\n" + "=" * 68)
    print(f"[OK] Готово! Репозиторий создан в: {ROOT}")
    print("Дальнейшие действия:")
    print("  1. Открой GitHub Desktop -> File -> Add Local Repository...")
    print(f"  2. Выбери папку: {ROOT}")
    print("  3. Нажми синюю кнопку 'Publish repository' (галочка Private).")
    print("  4. Добавь Мишу в Settings -> Collaborators на GitHub.")
    print("=" * 68)

if __name__ == "__main__":
    main()