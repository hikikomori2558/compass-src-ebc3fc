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

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- триграммный поиск (Метрика №11)

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

-- Привязки: врач — свои, пациент — свои (без app_is_attending: иначе рекурсия RLS)
CREATE POLICY bindings_select ON doctor_patient_bindings FOR SELECT USING (
  app_current_role() = 'ADMIN_METHODOLOGIST'
  OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND doctor_id = app_current_user_id())
  OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
);
CREATE POLICY bindings_insert ON doctor_patient_bindings FOR INSERT WITH CHECK (
  (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND doctor_id = app_current_user_id())
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
    'adverse_events_and_risks', 'clinical_alerts_and_routing'
  ] LOOP
    EXECUTE format($f$CREATE POLICY %I_select ON %I FOR SELECT USING (
      app_current_role() = 'ADMIN_METHODOLOGIST'
      OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
      OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
    ) $f$, t, t);
    EXECUTE format($f$CREATE POLICY %I_insert ON %I FOR INSERT WITH CHECK (
      app_current_role() = 'ADMIN_METHODOLOGIST'
      OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
      OR (app_current_role() = 'PATIENT' AND patient_hash_id = app_current_user_id())
    ) $f$, t, t);
    EXECUTE format($f$CREATE POLICY %I_update ON %I FOR UPDATE USING (
      app_current_role() = 'ADMIN_METHODOLOGIST'
      OR (app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP') AND app_is_attending(patient_hash_id))
    ) $f$, t, t);
  END LOOP;
END $$;

-- Пользователи: врачи/админ видят реестр сотрудников (нужно для login-флоу и направлений),
-- пациент — только свою запись.
CREATE POLICY users_select ON users_auth FOR SELECT USING (
  app_current_role() IN ('DOCTOR_PSYCHIATRIST','DOCTOR_NEUROLOGIST','DOCTOR_GP','ADMIN_METHODOLOGIST','AUTH_SERVICE')
  OR (app_current_role() = 'PATIENT' AND user_id = app_current_user_id())
);

-- outbox: пишет бэкенд, читает релей — сервисный доступ без пациентского скопа
CREATE POLICY outbox_all ON critical_events_outbox FOR ALL TO compass_api USING (true) WITH CHECK (true);

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
