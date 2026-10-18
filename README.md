# COMPASS-HYBRID v2.6 — Команда 20-П «Rapira» (rapira-clinical-compass)

Клиническая система поддержки принятия врачебных решений (CDSS) и цифрового сопровождения терапии препаратом **«Авиандр®» (маритупирдин)**.

Полная проектная документация: [`docs/Документация_проекта_COMPASS-HYBRID_v2.6.docx`](docs/Документация_проекта_COMPASS-HYBRID_v2.6.docx) (+ зеркало `.md` для поиска).
Как пушить: **[`docs/GIT_WORKFLOW.md`](docs/GIT_WORKFLOW.md)** — короткая инструкция для терминала и VS Code.

---

## 1. Архитектура репозитория

```
rapira-clinical-compass/
├── c_core/                      # Уровень I: нативное ядро C11 -> libcompass_core.so (.so в git НЕ коммитим)
│   ├── compass_core.h           #    бинарный контракт (8 противопоказаний ОХЛП, DDI, эндокринные маски) — Михаил
│   ├── compass_core.c           #    СКФ CKD-EPI, Чайлд-Пью, РК4 PK 168ч, CYP2D6, voice DSP — Михаил
│   └── (план) pii_scrubber.c, simd_lexer.c — Всеволод
├── backend/                     # Python-контур
│   ├── bio_paths.py             #    единая карта путей биоконтура
│   ├── trajectory_clustering.py #    мост ctypes к C-ядру + 24D GMM/DTW кластеры К1–К5 — Михаил
│   ├── clinical_rule_engine.py  #    маршрутизация, Закон № 3185-1, Kintsugi DAM, удержание — Михаил
│   ├── api_router_bio.py        #    FastAPI-роутер (6 эндпоинтов + Swagger) — Михаил
│   └── (план) main.py основного бэкенда, БД 12 таблиц, WebSocket-алертинг, JSONLogic — Всеволод
├── clinical_data/               # Детерминированные клинические знания — Михаил
│   ├── clinical_knowledge_base.json       # 8 шкал с лицензиями, ОХЛП «Авиандра», коридоры РКИ
│   └── synthetic_100_ehr_vignettes.json   # корпус апробации (seed=2026, детерминированный)
├── benchmarks/                  # Апробация и графики — Михаил
│   ├── ehr_nlp_benchmark.py               # генератор корпуса + NLP-парсер + метрики
│   ├── generate_scientific_plots.py       # 3 научных графика
│   ├── PROTOCOL_APROBATION_2026.md        # протокол апробации (генерируется)
│   └── plots/                             # PNG/SVG 300 DPI (генерируются)
├── micro_nets/                  # 3 микросети управления (RouterNet / DissimulationDropoutNet / ClusterVerifyNet) — Всеволод
├── frontend_doctor/             # ЭМК-сайдбар 420px + дашборд врача — Всеволод
├── frontend_patient/            # PWA / Telegram Mini App пациента — Всеволод
├── docs/                        # документация проекта + GIT_WORKFLOW.md
└── scripts/                     # build_c_core.py, smoke_test_c_core.py, sync_git.py
```

Правило «кто где работает» — чтобы не ловить конфликты:

- **Всеволод (№1)**: `micro_nets/`, `frontend_doctor/`, `frontend_patient/`, основной `backend/` (FastAPI-сервер, БД, WebSocket, JSONLogic), `c_core/pii_scrubber.c`, `c_core/simd_lexer.c`.
- **Михаил (№2)**: `c_core/` (PK/CYP/DDI/voice), `backend/` bio-модули (`trajectory_clustering.py`, `clinical_rule_engine.py`, `api_router_bio.py`), `clinical_data/`, `benchmarks/`, `docs/` (клинические тексты).

---

## 2. Как запустить и проверить (из корня репозитория)

```bash
# 1. Окружение (один раз)
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install numpy matplotlib fastapi uvicorn pydantic

# 2. Сборка C-ядра (или Cmd+Shift+B / Ctrl+Shift+B в VS Code)
python3 scripts/build_c_core.py
python3 scripts/smoke_test_c_core.py            # бенчмарк отклика, Метрика №1

# 3. Прогоны биоконтура
python3 backend/clinical_rule_engine.py         # тест 3 эталонных сценариев
python3 benchmarks/ehr_nlp_benchmark.py         # 100 синтетических ЭМК -> протокол апробации
python3 benchmarks/generate_scientific_plots.py # графики -> benchmarks/plots/

# 4. Web-API (Swagger UI)
python3 backend/api_router_bio.py               # http://127.0.0.1:8000/docs

# 5. Инфраструктура и БД (Метрика №12, RLS, Zero-PII)
docker compose up -d --build                    # PostgreSQL 16 + Redis 7 (AOF) + API
# Схема применяется только при ПЕРВОМ старте чистого тома: поменял schema.sql —
# пересоздай стек с нуля: docker compose down -v && docker compose up -d --build
python3 backend/db/seed.py                      # демо-данные (2 врача, 3 пациента, шкалы, ОХЛП)
python3 scripts/test_rls.py                     # изоляция врач/пациент + Zero-PII
python3 benchmarks/test_pii_scrubber.py         # обезличивание ПДн (152-ФЗ): ~0.5 ГБ/с чистый C, ~300 МБ/с сквозные

# 6. Аутентификация и привязка пациента по QR (Условия 1–2)
python3 benchmarks/test_auth.py                 # 27 проверок: логин/refresh/RBAC/QR/bind
# Демо-врачи (после seed): neuro.ivlanov / psych.petrova / admin.metod,
# пароль у всех compass-demo-2026. Эндпоинты: /api/v1/auth/{login,refresh,me,invite,bind}
```

Все скрипты можно запускать из корня репозитория — пути к `.so`, JSON и графикам резолвит `backend/bio_paths.py`.
Встраивание биоконтура в основной бэкенд Всеволода:

```python
from api_router_bio import bio_router
app.include_router(bio_router)
```

---

## 3. Горячие клавиши в VS Code

- `Cmd+Shift+B` (macOS) / `Ctrl+Shift+B` (Windows/Linux) — скомпилировать `c_core/*.c` в `libcompass_core.so`.
- `Cmd+Shift+P` -> `Tasks: Run Task` — 7 задач:
  1. Собрать C-Core
  2. Синхронизировать с GitHub (`git pull --rebase`)
  3. Smoke-тест C-Core (+ бенчмарк отклика)
  4. Тест 3 эталонных сценариев
  5. Бенчмарк 100 синтетических ЭМК (протокол апробации)
  6. Научные графики -> `benchmarks/plots/`
  7. Запустить bio API сервер (Swagger :8000)

---

## 4. Журнал разработчиков

### Михаил (17.10.2026) — биоконтур «COMPASS-HYBRID v2.6»
Нативное ядро C11 (8 хард-стопов ОХЛП, СКФ, Чайлд-Пью, РК4-фармакокинетика 168 ч с CYP2D6, DDI, эндокринные маски, voice DSP), 24-мерные траектории GMM/DTW (кластеры К1–К5), база клинических знаний (8 шкал с лицензиями, ОХЛП, коридоры РКИ), движок маршрутизации с правовым триажом (ст. 23/29 Закона № 3185-1, Приказ № 1071), NLP-парсер 5 шаблонов ЭМК + корпус 100 синтетических виньеток с метриками (Recall 100% на противопоказаниях), FastAPI-роутер на 6 эндпоинтов.

Подробное описание и инструкции по запуску на ВМ по SSH: [`docs/BIO_CONTOUR_v26.md`](docs/BIO_CONTOUR_v26.md).

### Как мы работаем параллельно без конфликтов (коротко)
1. Сел за компьютер — сначала `git pull --rebase origin main` (или Task 2).
2. Коммитишь только в свои папки (раздел 1).
3. Законченный кусок (раз в 1–2 часа) -> короткий commit message -> `git push`.
4. Полная инструкция: [`docs/GIT_WORKFLOW.md`](docs/GIT_WORKFLOW.md) (терминал, VS Code, GitHub Desktop, разбор конфликтов).
