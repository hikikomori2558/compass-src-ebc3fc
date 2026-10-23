# COMPASS-HYBRID v2.6 — Команда 20-П «Rapira» (rapira-clinical-compass)

Клиническая система поддержки принятия врачебных решений (CDSS) и цифрового сопровождения терапии препаратом **«Авиандр®» (маритупирдин)**.

Полная проектная документация: [`docs/COMPASS-HYBRID_v2.6-project-documentation.docx`](docs/COMPASS-HYBRID_v2.6-project-documentation.docx) — единственный источник (`.md`-зеркало удалено).
Как пушить: **[`docs/GIT_WORKFLOW.md`](docs/GIT_WORKFLOW.md)** — короткая инструкция для терминала и VS Code.

---

## 1. Архитектура репозитория

Слои backend идут строго вниз: `api/ -> services/ -> domain/ -> bridges/ -> paths`.
Решение о противопоказаниях принимает **только** декларативный движок правил
(`domain/rules.py` + `rules/*.json`); маршрутизация и legacy-пути его не дублируют.

```
rapira-clinical-compass/
├── c_core/                      # Уровень I: нативное ядро C11 -> libcompass_core.so
│   ├── compass_core.c/.h        #   СКФ CKD-EPI, Чайлд-Пью, РК4 PK 168ч, DDI, voice DSP
│   ├── pk_rk45_solver.c         #   Дорман—Принс RK45(5)4, Михаэлис—Ментен, CYP2D6/3A4
│   ├── bitap_ru_lexer.c/.h      #   НЕЧЁТКИЙ лексер ЭМК (Bitap/Wu-Manber k<=2, UTF-8 RU)
│   ├── simd_lexer.c/.h          #   ТОЧНЫЙ лексер (Ахо—Корасик): предразметка сущностей
│   ├── ot_xai_mahalanobis.c     #   Sinkhorn OT, Ledoit—Wolf, Оуэн-Шепли (XAI, ГОСТ 59921)
│   ├── bio_dsp_dst.c            #   LPC-4/TKEO/Weibull DSP + Демпстер—Шефер
│   ├── micronets_int8.c         #   W8A8 INT8-инференс микросетей + Mondrian RAPS
│   ├── pii_scrubber.c           #   потоковое HMAC-обезличивание ПДн (152-ФЗ)
│   ├── CMakeLists.txt, Makefile #   сборка (флаги = scripts/build_c_core.py; README.md — карта)
├── backend/                     # Python-контур (слои, см. backend/__init__.py)
│   ├── main.py                  #   FastAPI-сервер, телеметрия, CORS
│   ├── paths.py                 #   единая карта путей репозитория
│   ├── api/                     #   роутеры: bio, auth, alerts, cdss_workstation
│   ├── services/                #   pipeline (safe-путь «текст ЭМК -> решение»),
│   │                            #   ehr_extract (tri-state парсер), auth, alerts, bio_control
│   ├── domain/                  #   rules (хард-стопы, SSOT), routing (3185-1),
│   │                            #   trajectory (24D, К1–К5), evidence_xai (SHAP+CBR)
│   ├── bridges/                 #   ЕДИНСТВЕННЫЙ слой FFI: loader (одна загрузка .so),
│   │                            #   core, lexers (Ахо—Корасик + Bitap/PK), ot_xai,
│   │                            #   bio_native, bio_acoustic, bio_fusion, pii
│   ├── db/                      #   schema.sql (24+ таблицы, RLS), context.py, seed.py
│   └── rules/                   #   aviandr_hard_stops.json — правила как данные (v1.1.0)
├── clinical_data/               # Детерминированные клинические знания
│   ├── clinical_knowledge_base.json       # 8 шкал с лицензиями, ОХЛП «Авиандра», коридоры РКИ
│   ├── synthetic_100_ehr_vignettes.json   # корпус апробации (seed=2026)
│   └── kintsugi_dam_offline_calibration.json  # статическая калибровка голосового модуля
│                                             # (статус: рабочая гипотеза; вне клинических решений)
├── micro_nets/                  # Контур II: микросети управления (INT8 C-рантайм + conformal RAPS)
├── frontend_doctor/             # АРМ врача: OpenMRS 3 / Carbon / CDS Hooks + CPOE
│   └── clinical_engines_client.js  # клиентский слой: JWT-вход, WS-алерты, C11-движки
│                                 # (приоритет — бэкенд; офлайн-фоллбэк помечен честно)
├── frontend_patient/            # PWA-дневник пациента (Условие 2: QR-привязка без третьих лиц)
├── benchmarks/                  # Апробация: ehr_nlp_benchmark, run_holdout, графики, протокол
├── tests/                       # Функциональные тесты и смоуки (запуск: python3 -m tests.<имя>)
├── docs/                        # документация проекта + GIT_WORKFLOW.md
└── scripts/                     # build_c_core.py, demo_debian.sh, load_probe.py
```

Правило «кто где работает» — чтобы не ловить конфликты:

- **Всеволод (№1)**: `micro_nets/`, `frontend_doctor/`, основной `backend/` (инфраструктурные слои api/services/db), `c_core/pii_scrubber.c`, `c_core/simd_lexer.c`.
- **Михаил (№2)**: `c_core/` (PK/CYP/DDI/voice), `backend/domain/` + `backend/bridges/`, `clinical_data/`, `benchmarks/`, `docs/` (клинические тексты).

---

## 2. Как запустить и проверить (из корня репозитория)

```bash
# 1. Окружение (один раз)
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# 2. Сборка C-ядра (или Cmd+Shift+B / Ctrl+Shift+B в VS Code)
python3 scripts/build_c_core.py               # либо: make -C c_core / cmake -S c_core -B c_core/build
python3 -m tests.test_c_core_smoke            # бенчмарк отклика, Метрика №1

# 3. Функциональные тесты (без БД)
python3 -m tests.test_domain_rules            # декларативный движок хард-стопов
python3 -m tests.test_safe_pipeline           # tri-state разбор ЭМК + safe-конвейер (81+ проверок)
python3 -m tests.test_c_lexer_ahocorasik      # оракул vs наивный поиск на 100 виньетках
python3 -m tests.test_c_pii_scrubber          # обезличивание ПДн (152-ФЗ), ~0.5 ГБ/с чистый C
python3 -m tests.test_c_pk_rk45_bitap         # PK RK45 + нечёткий Bitap-лексер
python3 -m tests.test_c_ot_xai_ood            # Sinkhorn OT + Махаланобис OOD + Оуэн
python3 -m tests.test_bio_acoustic_micronets  # акустика + Демпстер—Шефер + INT8 микросети
python3 -m tests.test_api_cdss_workstation    # эндпоинт АРМ врача (CDS Hooks)

# 4. Демо биоконтура
python3 -m backend.services.pipeline          # 3 эталонных сценария (safe-путь)
python3 -m benchmarks.ehr_nlp_benchmark       # 100 синтетических ЭМК -> протокол апробации
python3 -m benchmarks.run_holdout             # фиксированный контрольный набор (0 опасных ошибок)
MPLBACKEND=Agg python3 -m benchmarks.generate_scientific_plots   # графики -> benchmarks/plots/

# 5. Web-API (Swagger UI)
python3 -m backend.main                       # http://127.0.0.1:8000/docs
scripts/api_smoke.sh                          # живое тыканье в API: 7 проверок эндпоинтов
# ВНИМАНИЕ: клинические эндпоинты требуют Bearer-токен (152-ФЗ):
# POST /api/v1/auth/login {"login":"neuro.ivlanov","password":"compass-demo-2026"}
# -> access_token -> кнопка Authorize в Swagger. Горячая загрузка шкал — только ADMIN_METHODOLOGIST.

# 6. Инфраструктура и БД (Метрика №12, RLS, Zero-PII)
docker compose up -d --build                  # PostgreSQL 16 + Redis 7 (AOF) + API
# Схема применяется только при ПЕРВОМ старте чистого тома: поменял schema.sql —
# пересоздай стек с нуля: docker compose down -v && docker compose up -d --build
python3 -m backend.db.seed                    # демо-данные (2 врача, 3 пациента, шкалы, ОХЛП)
python3 -m tests.test_db_rls                  # изоляция врач/пациент + Zero-PII
python3 -m tests.test_api_auth                # 27 проверок: логин/refresh/RBAC/QR/bind
python3 -m tests.test_api_alerts_ws           # 17 проверок: чек-ин -> WS врачу ~12 мс

# 7. Фронтенды (стенд разработки)
python3 -m http.server 5500 -d frontend_doctor    # АРМ врача: http://127.0.0.1:5500
python3 -m http.server 5501 -d frontend_patient   # PWA пациента: http://127.0.0.1:5501
# Вход врача: neuro.ivlanov / compass-demo-2026 (после seed) — движки переключаются
# на C11-бэкенд, открывается WS-канал алертов; без входа — честный офлайн-фоллбэк.

# 8. Нагрузочный зонд (честные RPS и p50/p95 на слайд Метрики №6)
python3 scripts/load_probe.py --scenario analyze --concurrency 32 --seconds 15

# 9. Полный демо-стенд на Debian-ноде (Tailscale): сборка, docker, БД, все suite — одной командой
./scripts/demo_debian.sh
```

Все команды запускаются из корня репозитория: пути к `.so`, JSON и графикам
резолвит `backend/paths.py`. Встраивание биоконтура в другое приложение:

```python
from backend.api.bio import bio_router
app.include_router(bio_router)
```

Графики апробации (`benchmarks/plots/`) — генерируемые артефакты, в git не хранятся
(строит `python3 -m benchmarks.generate_scientific_plots`). Протокол апробации
`benchmarks/PROTOCOL_APROBATION_2026.md` генерируется из того же прогона.

---

## 3. Горячие клавиши в VS Code

- `Cmd+Shift+B` (macOS) / `Ctrl+Shift+B` (Windows/Linux) — скомпилировать `c_core/*.c` в `libcompass_core.so`.
- `Cmd+Shift+P` -> `Tasks: Run Task` — 14 задач: сборка C-ядра, git pull --rebase,
  смоук ядра, демо-сценарии, бенчмарк, графики, API-сервер, docker-стек, seed и тесты.

---

## 4. Журнал разработчиков

### Михаил (17.10.2026) — биоконтур «COMPASS-HYBRID v2.6»
Нативное ядро C11 (8 хард-стопов ОХЛП, СКФ, Чайлд-Пью, РК4-фармакокинетика 168 ч с CYP2D6, DDI, эндокринные маски, voice DSP), 24-мерные траектории со стратификацией К1–К5 (детерминированные клинические правила + DTW до коридора РКИ), база клинических знаний (8 шкал с лицензиями, ОХЛП, коридоры РКИ), движок маршрутизации с правовым триажом (ст. 23/29 Закона № 3185-1, Приказ № 1071), NLP-парсер 5 шаблонов ЭМК + корпус 100 синтетических виньеток с метриками (Recall 100% на противопоказаниях), FastAPI-роутер на 6 эндпоинтов.

Подробное описание и инструкции по запуску на ВМ по SSH: [`docs/BIO_CONTOUR_v26.md`](docs/BIO_CONTOUR_v26.md).

### Как мы работаем параллельно без конфликтов (коротко)
1. Сел за компьютер — сначала `git pull --rebase origin main` (или Task 2).
2. Коммитишь только в свои папки (раздел 1).
3. Законченный кусок (раз в 1–2 часа) -> короткий commit message -> `git push`.
4. Полная инструкция: [`docs/GIT_WORKFLOW.md`](docs/GIT_WORKFLOW.md) (терминал, VS Code, GitHub Desktop, разбор конфликтов).
