-- ============================================================================
-- COMPASS-HYBRID v2.6 (команда 20-П «Rapira») — схема БД: 12 клинических сущностей
-- (Метрика №12 «Полнота модели данных», раздел 6.6 документации)
--
-- Применяется автоматически при первом старте контейнера PostgreSQL 16
-- (docker-entrypoint-initdb.d). Роли:
--   compass_app — суперпользователь кластера (миграции, seed)
--   compass_api — роль приложения (ОБХОДИТ RLS НЕ МОЖЕТ — см. политики ниже)
--
-- Zero-PII Clinical Core: patient_pii_vault недоступен роли приложения вовсе —
-- клинический контур оперирует только patient_hash_id (152-ФЗ, ст. 13 323-ФЗ).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- триграммный поиск (Метрика №11)
-- gen_random_uuid() входит в ядро PostgreSQL 13+, поэтому pgcrypto не нужен.

-- ----------------------------------------------------------------------------
-- Роль приложения: НЕ суперпользователь → RLS-политики для неё обязательны
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'compass_api') THEN
    CREATE ROLE compass_api LOGIN PASSWORD 'compass_api_demo';
  END IF;
END $$;

-- ----------------------------------------------------------------------------
-- Служебные функции контекста (заполняются middleware бэкенда через set_config)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_current_role() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(NULLIF(current_setting('app.role', true), ''), 'NONE');
$$;

CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.user_id', true), '')::uuid;
$$;

-- Двойная семантика app.user_id (важно для middleware бэкенда):
--   для ролей DOCTOR_*/ADMIN — это users_auth.user_id;
--   для роли PATIENT — это patient_hash_id из вальта (у пациента нет записи в users_auth).

-- ============================================================================
-- 1/12. users_auth & rbac_roles (роли DOCTOR_*, PATIENT, ADMIN_METHODOLOGIST, MFA)
-- ============================================================================
CREATE TABLE rbac_roles (
  role_code  text PRIMARY KEY,
  title_ru   text NOT NULL
);

CREATE TABLE users_auth (
  user_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role_code     text NOT NULL REFERENCES rbac_roles(role_code),
  login         text UNIQUE NOT NULL,
  password_hash text NOT NULL,                -- argon2id (бэкенд)
  full_name     text NOT NULL,
  mfa_enabled   boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Неизменяемый журнал аудита доступа (ст. 13 323-ФЗ): пишет только INSERT
CREATE TABLE audit_log (
  audit_id   bigserial PRIMARY KEY,
  actor_id   uuid,
  action     text NOT NULL,
  entity     text,
  entity_id  text,
  details    jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Zero-PII: изолированное хранилище ПДн. Шифрование на прикладном уровне
-- (AES-256-GCM / ГОСТ «Кузнечик», ключ в KMS/env, key_version для ротации).
-- Наружу — только псевдоним patient_hash_id.
-- ----------------------------------------------------------------------------
CREATE TABLE patient_pii_vault (
  patient_hash_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fio_encrypted   bytea NOT NULL,
  phone_encrypted bytea,
  snils_encrypted bytea,
  birth_date      date,
  sex             smallint CHECK (sex IN (0, 1)),   -- 0 = Ж, 1 = М (как в C-ядре)
  key_version     smallint NOT NULL DEFAULT 1,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- 2/12. doctor_patient_bindings — лечащий врач ↔ пациент + история ИДС
CREATE TABLE doctor_patient_bindings (
  binding_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  doctor_id       uuid NOT NULL REFERENCES users_auth(user_id),
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  active          boolean NOT NULL DEFAULT true,
  consents        jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{type:'ИДС', signed_at, method:'QR'}]
  bound_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (doctor_id, patient_hash_id)
);
-- Частичный индекс под app_is_attending(): горячий вопрос «мой ли это пациент?»
-- решается по patient_hash_id среди активных привязок, без полного скана.
CREATE INDEX idx_bindings_patient_active ON doctor_patient_bindings (patient_hash_id) WHERE active;

-- SECURITY DEFINER: выполняется от владельца → нет рекурсии RLS на bindings
-- (создаётся ПОСЛЕ таблицы bindings — PostgreSQL валидирует связи SQL-функций при создании)
CREATE OR REPLACE FUNCTION app_is_attending(p_patient uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM doctor_patient_bindings b
    WHERE b.patient_hash_id = p_patient
      AND b.doctor_id = app_current_user_id()
      AND b.active
  );
$$;

-- 3/12. ehr_encounters_parsed — структурированные слепки историй болезни
CREATE TABLE ehr_encounters_parsed (
  encounter_id    bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  doctor_id       uuid REFERENCES users_auth(user_id),
  raw_text_hash   text,                                -- sha256 исходного текста (сам текст с ПДн не храним)
  parsed          jsonb NOT NULL,                      -- жалобы/анамнез/МКБ-10/виталы/СКФ/АЛТ/АСТ/Чайлд-Пью/CYP2D6
  icd10_primary   text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ehr_parsed_json ON ehr_encounters_parsed USING gin (parsed jsonb_path_ops);
CREATE INDEX idx_ehr_patient_time ON ehr_encounters_parsed (patient_hash_id, created_at DESC);

-- 4/12. psychometric_scales_registry — паспорта шкал (JSON-манифесты)
CREATE TABLE psychometric_scales_registry (
  scale_code text PRIMARY KEY,
  manifest   jsonb NOT NULL    -- items, cutoffs, DOI валидации, лицензия (из clinical_knowledge_base.json)
);

-- 5/12. patient_scale_assessments — результаты шкал во времени
CREATE TABLE patient_scale_assessments (
  assessment_id   bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  scale_code      text NOT NULL REFERENCES psychometric_scales_registry(scale_code),
  total_score     numeric NOT NULL,
  items           jsonb NOT NULL,                      -- попунктовой разбор
  delta_prev      numeric,                             -- дельта к предыдущему визиту
  taken_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_assessments_patient ON patient_scale_assessments (patient_hash_id, scale_code, taken_at DESC);

-- 6/12. multimodal_biomarkers_ts — временные ряды объективных маркеров
CREATE TABLE multimodal_biomarkers_ts (
  marker_id       bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  source          text NOT NULL CHECK (source IN ('VOICE_DAM', 'VOICE_DSP', 'RPPG', 'WEARABLE', 'CIRCADIAN')),
  values          jsonb NOT NULL                       -- {dep_class, anx_class, jitter, hrv_rmssd, baevsky...}
);
CREATE INDEX idx_biomarkers_patient_time ON multimodal_biomarkers_ts (patient_hash_id, recorded_at DESC);

-- 7/12. grls_drugs_ohpl_catalog — эталонный справочник ГРЛС/ОХЛП
CREATE TABLE grls_drugs_ohpl_catalog (
  drug_code text PRIMARY KEY,
  manifest  jsonb NOT NULL     -- показания МКБ-10, дозирование, противопоказания, DDI, PK, коридоры РКИ
);

-- 8/12. therapy_plans_and_prescriptions — планы, подтверждённые врачом
CREATE TABLE therapy_plans_and_prescriptions (
  plan_id                bigserial PRIMARY KEY,
  patient_hash_id        uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  doctor_id              uuid NOT NULL REFERENCES users_auth(user_id),
  drug_code              text NOT NULL REFERENCES grls_drugs_ohpl_catalog(drug_code),
  label_status           text NOT NULL CHECK (label_status IN ('IN_LABEL', 'OFF_LABEL', 'BLOCKED', 'HOLD')),
  dosing                 jsonb,                        -- {scheme:'20 мг 2 р/сут', before_meal_min:30}
  confirmed_by_doctor_id uuid NOT NULL REFERENCES users_auth(user_id),
  confirmed_at           timestamptz NOT NULL DEFAULT now(),
  status                 text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','COMPLETED','CANCELLED'))
);

-- 9/12. medication_adherence_logs — дневник приёма (PDC, КОП-25, «за 30 мин до еды»)
CREATE TABLE medication_adherence_logs (
  log_id           bigserial PRIMARY KEY,
  patient_hash_id  uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  plan_id          bigint REFERENCES therapy_plans_and_prescriptions(plan_id),
  planned_at       timestamptz NOT NULL,
  taken_at         timestamptz,
  before_meal_30min boolean,
  dose_skipped     boolean NOT NULL DEFAULT false,
  pdc_pct          numeric,
  kop25_score      smallint CHECK (kop25_score BETWEEN 0 AND 100),
  intake_time      text,                                -- время приёма (HH:MM) — фенотипная аналитика
  notes            text,                                -- жалоба/побочка словами пациента (-> врачу)
  diary            jsonb,                               -- подробный дневник состояния
  event_uuid       uuid UNIQUE                          -- идемпотентность офлайн-досылки PWA (RPO=0)
);
CREATE INDEX idx_adherence_patient_time ON medication_adherence_logs (patient_hash_id, planned_at DESC);

-- 10/12. adverse_events_and_risks — реестр НЯ (MedDRA/CTCAE) + черновик извещения № 1071
CREATE TABLE adverse_events_and_risks (
  ae_id           bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  meddra_term     text NOT NULL,
  severity_ctcae  smallint CHECK (severity_ctcae BETWEEN 1 AND 5),
  related_to_drug text CHECK (related_to_drug IN ('DEFINITELY','PROBABLE','POSSIBLE','UNLIKELY','UNRELATED')),
  transient_ae    boolean,
  roszdravnadzor_draft jsonb,                          -- черновик извещения (Приказ № 1071)
  reported_at     timestamptz NOT NULL DEFAULT now()
);

-- 11/12. clinical_alerts_and_routing — журнал алертинга и маршрутизации
CREATE TABLE clinical_alerts_and_routing (
  alert_id        bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  category        text NOT NULL CHECK (category IN ('MANDATORY_PSYCHIATRIST','RECOMMENDED_PSYCHIATRIST','PRIMARY_CARE_MANAGEMENT')),
  critical        boolean NOT NULL DEFAULT false,
  reasons         jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  delivered_to_doctor_at timestamptz,
  delivery_ms     integer,                             -- Метрика №5/алертинг: доставка < 200 мс
  doctor_reaction_at     timestamptz,
  action_taken    text
);
CREATE INDEX idx_alerts_patient_time ON clinical_alerts_and_routing (patient_hash_id, created_at DESC);

-- ----------------------------------------------------------------------------
-- 12/12. Transactional Outbox — гарантированная доставка критических алертов
-- (RPO = 0: запись в outbox в той же транзакции, что и событие; релей → Redis Streams)
-- ----------------------------------------------------------------------------
CREATE TABLE critical_events_outbox (
  event_id           bigserial PRIMARY KEY,
  event_uuid         uuid UNIQUE NOT NULL,
  payload            jsonb NOT NULL,
  published_to_redis boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);
-- Релей забирает только неопубликованное, и с каждым сбоем сети их становится больше —
-- без частичного индекса он бы сканировал всю историю outbox'а.
CREATE INDEX idx_outbox_unpublished ON critical_events_outbox (created_at) WHERE NOT published_to_redis;

-- ----------------------------------------------------------------------------
-- Доработки по чек-листу дорабора кейса (P0): согласия, коммуникации, доставка
-- уведомлений, версионирование правил, источники обоснований, вовлечённость.
-- ----------------------------------------------------------------------------

-- ИДС: без записи согласия пациентский контур не активируется (152-ФЗ ст. 10,
-- 323-ФЗ ст. 13). Версия текста фиксируется — согласие относится к конкретной редакции.
CREATE TABLE consents (
  consent_id      bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  consent_type    text NOT NULL CHECK (consent_type IN ('PERSONAL_DATA_PROCESSING','TELEMONITORING','VOICE_PROCESSING','VIDEO_RPPG','RESEARCH')),
  text_version    text NOT NULL,
  granted_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz,
  revoke_reason   text
);
CREATE INDEX idx_consents_patient ON consents (patient_hash_id, consent_type) WHERE revoked_at IS NULL;

-- Задача 6: асинхронный чат врач-пациент. Тексты пациента проходят пред-триаж,
-- но ответ клинического характера пишет только врач (Условие 3).
CREATE TABLE messages (
  message_id      bigserial PRIMARY KEY,
  patient_hash_id uuid NOT NULL REFERENCES patient_pii_vault(patient_hash_id),
  sender_role     text NOT NULL CHECK (sender_role IN ('PATIENT','DOCTOR','SYSTEM_INFO')),
  sender_id       uuid REFERENCES users_auth(user_id),
  thread_kind     text NOT NULL DEFAULT 'CLINICAL' CHECK (thread_kind IN ('CLINICAL','REGIMEN','SOCIAL')),
  priority        smallint NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 3), -- 3 = клинически важно
  body            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_patient_time ON messages (patient_hash_id, created_at DESC);

CREATE TABLE message_receipts (
  message_id  bigint PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
  delivered_at timestamptz,
  read_at     timestamptz
);

-- Доставка уведомлений врачу по каналам. Ключевая честная метрика алертинга —
-- «время до подтверждения человеком», а не миллисекунды доставки байта.
CREATE TABLE notification_deliveries (
  delivery_id  bigserial PRIMARY KEY,
  alert_id     bigint REFERENCES clinical_alerts_and_routing(alert_id),
  channel      text NOT NULL CHECK (channel IN ('WEBSOCKET','WEB_PUSH','SMS_GATEWAY','EMAIL')),
  sent_at      timestamptz,
  delivered_at timestamptz,
  confirmed_by_doctor_at timestamptz,   -- вот она, метрика Условия 5/6
  payload_kind text NOT NULL DEFAULT 'EMPTY_PING'  -- в канале только «откройте систему», без клиники
);

-- Метрика 10: правила и шкалы версионируются. Смена поведения системы = новая версия
-- записи, а не правка кода; каждая версия проходит регрессионный набор перед активацией.
CREATE TABLE rule_versions (
  rule_set_id   text NOT NULL,          -- 'aviandr_hard_stops', 'alert_routing_dsl', ...
  version       text NOT NULL,
  content_sha256 text NOT NULL,
  is_active     boolean NOT NULL DEFAULT false,
  regression_passed boolean NOT NULL DEFAULT false,
  activated_by  uuid REFERENCES users_auth(user_id),  -- методист, не система сама
  activated_at  timestamptz,
  PRIMARY KEY (rule_set_id, version)
);

CREATE TABLE scale_versions (
  scale_code text NOT NULL,
  version    text NOT NULL,
  content_sha256 text NOT NULL,
  is_active  boolean NOT NULL DEFAULT false,
  PRIMARY KEY (scale_code, version)
);

-- Метрика 5 (обоснованность): каждая рекомендация ссылается на источник с версией
-- и датой сверки по первоисточнику. Ни одной рекомендации без строки здесь.
CREATE TABLE guideline_sources (
  source_id   text PRIMARY KEY,         -- 'AVIANDR_OHLP', 'KR_GTR_2024', ...
  doc_title   text NOT NULL,
  doc_ref     text NOT NULL,            -- номер РУ / ID в рубрикаторе КР / DOI
  verified_url text NOT NULL,
  verified_at date NOT NULL,
  note        text
);

-- Вовлечённость пациента (см. docs/PRODUCT_DESIGN.md): серии и скоринг как у Duolingo —
-- мягкая игровая механика против дропаута на 1-2-й неделе. Данные обезличены (hash_id).
CREATE TABLE patient_engagement (
  patient_hash_id uuid PRIMARY KEY REFERENCES patient_pii_vault(patient_hash_id),
  streak_days     integer NOT NULL DEFAULT 0,
  best_streak     integer NOT NULL DEFAULT 0,
  daily_score     smallint NOT NULL DEFAULT 0 CHECK (daily_score BETWEEN 0 AND 100),
  total_points    integer NOT NULL DEFAULT 0,
  last_checkin_date date,
  badges          jsonb NOT NULL DEFAULT '[]'::jsonb
);

-- Условие 3: hard stop не блокирует врача физически. Любой override — с причиной,
-- автором и попадает в неизменяемый аудит (см. audit_log).
ALTER TABLE therapy_plans_and_prescriptions
  ADD COLUMN override_reason text,
  ADD COLUMN overridden_by uuid REFERENCES users_auth(user_id),
  ADD COLUMN override_confirmed_at timestamptz;

-- ----------------------------------------------------------------------------
-- Привязка пациента по QR (день 5, Условия 1–2). Приглашение одноразовое, TTL 24 ч
-- (требование чек-листа 1.1). Храним jti и sha256 полного токена: сам токен нигде
-- не persists, однократность гарантирует атомарный UPDATE ... WHERE used_at IS NULL.
-- Таблица сервисная (создаёт врач, поглощает /auth/bind) — сознательно без RLS.
CREATE TABLE patient_invite_tokens (
  jti             uuid PRIMARY KEY,
  token_sha256    text NOT NULL,
  doctor_id       uuid NOT NULL REFERENCES users_auth(user_id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,
  used_at         timestamptz,
  used_by_patient uuid REFERENCES patient_pii_vault(patient_hash_id)
);
CREATE INDEX idx_invites_open ON patient_invite_tokens (doctor_id) WHERE used_at IS NULL;

-- Единственная дверь роли приложения в PII-вальт: INSERT-ONLY через SECURITY DEFINER.
-- compass_api не имеет SELECT на patient_pii_vault (см. гранты ниже), а новый пациент
-- без этой функции был бы невозможен. Пустые шифротексты + key_version 0 = «реквизиты
-- пациент ещё не внёс» (внесёт сам через PWA — записью, не чтением вальта).
CREATE OR REPLACE FUNCTION create_patient_vault_entry() RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  INSERT INTO patient_pii_vault (fio_encrypted, phone_encrypted, snils_encrypted, key_version)
  VALUES (''::bytea, ''::bytea, ''::bytea, 0)
  RETURNING patient_hash_id;
$$;
REVOKE ALL ON FUNCTION create_patient_vault_entry() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION create_patient_vault_entry() TO compass_api;




-- ============================================================================
-- Row-Level Security: врач видит ТОЛЬКО своих пациентов, пациент — только себя.
-- Fail-closed: без set_config('app.role'/'app.user_id') выборка пуста.
-- ============================================================================
ALTER TABLE doctor_patient_bindings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctor_patient_bindings        FORCE  ROW LEVEL SECURITY;
ALTER TABLE ehr_encounters_parsed          ENABLE ROW LEVEL SECURITY;
ALTER TABLE ehr_encounters_parsed          FORCE  ROW LEVEL SECURITY;
ALTER TABLE patient_scale_assessments      ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_scale_assessments      FORCE  ROW LEVEL SECURITY;
ALTER TABLE multimodal_biomarkers_ts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE multimodal_biomarkers_ts       FORCE  ROW LEVEL SECURITY;
ALTER TABLE therapy_plans_and_prescriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE therapy_plans_and_prescriptions FORCE  ROW LEVEL SECURITY;
ALTER TABLE medication_adherence_logs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE medication_adherence_logs      FORCE  ROW LEVEL SECURITY;
ALTER TABLE adverse_events_and_risks       ENABLE ROW LEVEL SECURITY;
ALTER TABLE adverse_events_and_risks       FORCE  ROW LEVEL SECURITY;
ALTER TABLE clinical_alerts_and_routing    ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinical_alerts_and_routing    FORCE  ROW LEVEL SECURITY;
ALTER TABLE consents                       ENABLE ROW LEVEL SECURITY;
ALTER TABLE consents                       FORCE  ROW LEVEL SECURITY;
ALTER TABLE messages                       ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages                       FORCE  ROW LEVEL SECURITY;
ALTER TABLE patient_engagement             ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_engagement             FORCE  ROW LEVEL SECURITY;

-- Вовлечённость: пациент обновляет СВОЙ ряд (UPSERT из чекина; PG16 отвергает
-- ON CONFLICT при FORCE RLS — приложение использует UPDATE→INSERT)
CREATE POLICY engagement_update_patient ON patient_engagement FOR UPDATE USING (
  app_current_role() = 'ADMIN_METHODOLOGIST'
  OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
  OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
);

-- Привязки: врач — свои, пациент — свои (без app_is_attending: иначе рекурсия RLS)
CREATE POLICY bindings_select ON doctor_patient_bindings FOR SELECT USING (
  app_current_role() = 'ADMIN_METHODOLOGIST'
  OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND doctor_id = app_current_user_id())
  OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
);
CREATE POLICY bindings_insert ON doctor_patient_bindings FOR INSERT WITH CHECK (
  (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND doctor_id = app_current_user_id())
  -- AUTH_SERVICE — контекст эндпоинта /auth/bind: создаёт привязку по одноразовому QR,
  -- doctor_id берётся из подписанного токена приглашения, а не из контекста сессии
  OR (app_current_role() = 'AUTH_SERVICE')
);
CREATE POLICY bindings_update ON doctor_patient_bindings FOR UPDATE USING (
  app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND doctor_id = app_current_user_id()
);

-- Все пациентско-скопированные таблицы: одна логика — генерируем политики циклом
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'ehr_encounters_parsed', 'patient_scale_assessments', 'multimodal_biomarkers_ts',
    'therapy_plans_and_prescriptions', 'medication_adherence_logs',
    'adverse_events_and_risks', 'clinical_alerts_and_routing',
    'consents', 'messages', 'patient_engagement'
  ] LOOP
    EXECUTE format($f$CREATE POLICY %I_select ON %I FOR SELECT USING (
      app_current_role() = 'ADMIN_METHODOLOGIST'
      OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
      OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
    ) $f$, t, t);
    EXECUTE format($f$CREATE POLICY %I_insert ON %I FOR INSERT WITH CHECK (
      app_current_role() = 'ADMIN_METHODOLOGIST'
      OR app_current_role() = 'AUTH_SERVICE'
      OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
      OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
    ) $f$, t, t);
    EXECUTE format($f$CREATE POLICY %I_update ON %I FOR UPDATE USING (
      app_current_role() = 'ADMIN_METHODOLOGIST'
      OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
    ) $f$, t, t);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- СОЗНАТЕЛЬНО БЕЗ RLS (не удаляйте эти комментарии при ревью):
--   users_auth — приложению нужен password_hash для проверки логина (argon2
--   верифицируется на стороне бэкенда), а врачам — реестр коллег для направлений.
--   Пациентские ПДн здесь не хранятся: пациенты в users_auth не заводятся,
--   их идентичность — patient_hash_id из вальта (см. привязки по QR).
--   audit_log — append-only журнал безопасности, запись без чтения (см. GRANT).
--   critical_events_outbox — служебная очередь релея, clinical_payload содержит
--   только обезличенные patient_hash_id.
--   rbac_roles, psychometric_scales_registry, grls_drugs_ohpl_catalog — справочники.
-- ----------------------------------------------------------------------------


-- ----------------------------------------------------------------------------
-- Права роли приложения. patient_pii_vault НЕ выдаётся вообще (Zero-PII).
-- ----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO compass_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO compass_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO compass_api;
REVOKE ALL ON patient_pii_vault FROM compass_api;
REVOKE ALL ON audit_log FROM compass_api;
GRANT INSERT ON audit_log TO compass_api;

INSERT INTO rbac_roles (role_code, title_ru) VALUES
  ('DOCTOR_PSYCHIATRIST',  'Врач-психиатр'),
  ('DOCTOR_NEUROLOGIST',   'Врач-невролог'),
  ('DOCTOR_GP',            'Врач-терапевт (общая практика)'),
  ('PATIENT',              'Пациент'),
  ('ADMIN_METHODOLOGIST',  'Администратор-методист (врач-методист, No-Code конструктор)'),
  ('AUTH_SERVICE',         'Служебная роль сервиса аутентификации')
ON CONFLICT (role_code) DO NOTHING;

-- ----------------------------------------------------------------------------
-- Дневник пациента (контур сопровождения, День 7+): время приёма, побочки
-- с пояснениями в момент возникновения, настроение/сон, заметки.
-- Одна запись на пациента в сутки (UNIQUE) — правки перезаписывают, история честная.
-- ----------------------------------------------------------------------------
CREATE TABLE patient_diary_entries (
  entry_id          bigserial PRIMARY KEY,
  patient_hash_id   uuid NOT NULL,
  entry_date        date NOT NULL DEFAULT current_date,
  intake_time       time,                          -- когда принял (фенотип + RMSE «за 30 мин до еды»)
  taken             boolean NOT NULL DEFAULT true, -- принял ли дозу
  before_meal_30min boolean,                       -- выдержал правило «за 30 мин до еды»
  side_effects      jsonb NOT NULL DEFAULT '[]',   -- [{"code","severity","note"}]
  mood_score        int CHECK (mood_score BETWEEN 0 AND 10),
  anxiety_score     int CHECK (anxiety_score BETWEEN 0 AND 10),
  sleep_score       int CHECK (sleep_score BETWEEN 0 AND 10),
  notes             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (patient_hash_id, entry_date)
);
CREATE INDEX idx_diary_patient_date ON patient_diary_entries (patient_hash_id, entry_date DESC);
ALTER TABLE medication_adherence_logs ADD COLUMN IF NOT EXISTS intake_time time;
ALTER TABLE medication_adherence_logs ADD COLUMN IF NOT EXISTS notes text;
ALTER TABLE medication_adherence_logs ADD COLUMN IF NOT EXISTS diary jsonb DEFAULT '{}';
GRANT SELECT, INSERT, UPDATE, DELETE ON patient_diary_entries TO compass_api;
-- роли врачей/пациента/методиста (RLS-контексты SET ROLE) тоже работают с дневником
DO $$ DECLARE r text;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'DOCTOR%' OR rolname IN ('PATIENT','ADMIN_METHODOLOGIST')
  LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON patient_diary_entries TO %I', r);
  END LOOP;
END $$;
GRANT USAGE, SELECT ON SEQUENCE patient_diary_entries_entry_id_seq TO compass_api;

-- ----------------------------------------------------------------------------
-- v7: профили кластеризации пациентов — персист 24D-вектора, кластера, проекции,
-- DTW и OOD-отчёта (c_core/ot_xai_mahalanobis.c). Наполняется при analyze-and-route
-- привязанного пациента и бэкфиллом из истории шкал/приверженности.
CREATE TABLE IF NOT EXISTS patient_cluster_profiles (
  profile_id       bigserial PRIMARY KEY,
  patient_hash_id  uuid NOT NULL UNIQUE REFERENCES patient_pii_vault(patient_hash_id),
  z24              jsonb NOT NULL,
  cluster_code     text NOT NULL,
  projection_x     real NOT NULL,
  projection_y     real NOT NULL,
  dtw              real,
  ood              jsonb,
  computed_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pcp_cluster ON patient_cluster_profiles (cluster_code);
ALTER TABLE patient_cluster_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_cluster_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY pcp_select ON patient_cluster_profiles FOR SELECT USING (
  app_current_role() = 'ADMIN_METHODOLOGIST'
  OR app_current_role() = 'AUTH_SERVICE'
  OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
  OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
);
CREATE POLICY pcp_insert ON patient_cluster_profiles FOR INSERT WITH CHECK (
  app_current_role() = 'ADMIN_METHODOLOGIST'
  OR app_current_role() = 'AUTH_SERVICE'
  OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
);
CREATE POLICY pcp_update ON patient_cluster_profiles FOR UPDATE USING (
  app_current_role() = 'ADMIN_METHODOLOGIST' OR app_current_role() = 'AUTH_SERVICE'
);
GRANT SELECT, INSERT, UPDATE, DELETE ON patient_cluster_profiles TO compass_api;
GRANT USAGE, SELECT ON SEQUENCE patient_cluster_profiles_profile_id_seq TO compass_api;
DO $$ DECLARE r text;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'DOCTOR%' OR rolname IN ('PATIENT','ADMIN_METHODOLOGIST','AUTH_SERVICE')
  LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON patient_cluster_profiles TO %I', r);
  END LOOP;
END $$;
