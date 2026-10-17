# COMPASS-HYBRID v2.6 — Команда 20-П «Rapira» (rapira-clinical-compass)

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
