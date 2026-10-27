<div align="center">

# COMPASS-HYBRID v2.6

**Цифровой ассистент врача для ведения пациента с тревожной и депрессивной симптоматикой**

Гибридная СППВ: расчёты на C99/SIMD-ядре, правила по ОХЛП и КР МЗ РФ,
конформно-калиброванные микросети. Решение — всегда за врачом.

**Команда Rapira · 20-П · Чемпионат «Клинический компас», финал**

</div>

---

> ⚠️ **Статус репозитория: приватный.** Исходный код распространяется участникам
> жюри по QR-коду с презентации (архив с этого стенда) и не предназначен для
> публичного использования. Все данные пациентов на стенде — синтетические.

## Что это

Три контура поверх реальной поликлинической ЭМК:

| Контур | Что делает | Папка |
|---|---|---|
| **ЭМК вендора** | «Чужая» поликлиническая система (порт 8100) — COMPASS её не изменяет | `backend_emc/` · `frontend_emc/` |
| **Панель врача** | Закладка поверх ЭМК: светофор безопасности, шкалы, кластеры траекторий, what-if CYP2D6, чат с пациентом, НЯ и извещение 1071, HAM-A-14 | `frontend_compass/` · `backend/api/` |
| **PWA пациента** | Чек-ин за 3 тапа, опросники GAD-7 (Г2) и HADS-14 (Г1) с пояснением в момент вопроса, дневник, связь с врачом | `frontend_patient/` |

## Быстрый старт (боевой стенд уже развёрнут)

```bash
# прод-стенд: https://projectc.work.gd  (демо-хаб: выбор пациента из 105)
# вход врача: neuro.ivlanov / compass-demo-2026

git clone git@github.com:hikikomori2558/rapira-clinical-compass.git
cd rapira-clinical-compass
cp .env.example .env            # заполнить JWT_SECRET, PII_PEPPER, POSTGRES_PASSWORD
docker compose up -d db redis
docker compose build api emc
docker compose --profile prod up -d   # + Caddy с TLS для домена
python3 -m backend.db.seed            # справочники + демо-врачи
python3 -m scripts.seed_105_patients  # 105 синтетических пациентов с ретроспективой
```

Требования: Docker + compose v2, 2 vCPU / 4 GB / 40 GB. Полное ТЗ — `scripts/vps/README.md`.

## Архитектура (всё локально, 0 внешних API)

```
ЭМК-текст ─▶ pii_scrubber.c ─▶ ehr_extract.py (Bitap/SIMD, три-стейт)
        ─▶ compass_core.c (СКФ CKD-EPI 2021, Чайлд-Пью, RK45, 13 правил ОХЛП)
        ─▶ ot_xai_mahalanobis.c (24D-кластеры K1–K5, Синкхорн-Вассерштейн, OOD)
        ─▶ micronets_int8.c (прототип, обучен на синтетике)
        ─▶ rules_engine.py (JSON-правила) ─▶ вердикт врачу ─▶ audit_log
```

* **~3 500 строк C** в 8 модулях (`c_core/`), 26 таблиц PostgreSQL, **12 с RLS**
* **322 автопроверки** в 15 сьютах, hold-out **0/24 опасных ошибок**
* **287 rps** боевого пути на 2 vCPU (p95 = 107 мс, 16 параллельных)
* Каждый факт в UI кликабелен до публикации (`docs/SOURCES.md`)

## Документация для жюри

| Документ | О чём |
|---|---|
| `docs/COMPLIANCE_MATRIX.md` | Соответствие всем пунктам кейса: 8 задач, 10 условий, 13 метрик |
| `docs/SOURCES.md` | 31 источник: РКИ, КР МЗ РФ, ИИ-литература — с верификацией |
| `docs/UX_UI_DESIGN.md` | Дизайн-система и принципы (правило 3 кликов) |
| `docs/THREAT_MODEL.md` | Модель угроз и защита ПДн (152-ФЗ) |
| `docs/FEATURE_AUDIT.md` | Сверка интерфейсов с задачами кейса |
| `docs/JURY_QA.md` | Частые вопросы жюри с ответами |

## Операционное

```bash
scripts/vps/bootstrap.sh        # полный деплой на чистый VPS (см. scripts/vps/README.md)
scripts/vps/deploy_from_mac.sh  # переехать/обновить стенд с машины разработки
scripts/backup_db.sh            # pg_dump (systemd-таймер 03:40)
scripts/load_test.py            # нагрузочный профиль (замер на боевом VPS)
scripts/purge_non_synthetic.sql # вернуть стенд к 105 синтетическим пациентам
```

Автодеплой: `git pull && docker compose build api && docker compose up -d api`.
Автостарт: у всех сервисов `restart: unless-stopped`; бэкап и синк — systemd-таймеры
(`scripts/compass-backup.timer`).

## Контакты команды

* **Букалов Всеволод** — архитектура ядра, дизайн, презентация · `t.me/bukalov_vs` · `bukalovvsevolod@gmail.com`
* **Журавлев Михаил** — ядро на C, NER, PK-калибровка, бэкенд · `zuravlevm701@gmail.com`

---

© 2026 Команда Rapira (20-П). Проприетарная разработка для Чемпионата
«Клинический компас». Не является открытым ПО; использование — только
в рамках процедуры Чемпионата. Данные пациентов на стенде синтетические.
