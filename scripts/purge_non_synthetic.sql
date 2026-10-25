-- Очистка стенда от несинтетических пациентов (после боевых тестов/ручных привязок).
-- Остаётся ровно рукописный набор из synthetic_personas.py (105).
-- Запуск: docker exec -i compass_db psql -U compass_app -d compass < scripts/purge_non_synthetic.sql
BEGIN;
CREATE TEMP TABLE oldp AS SELECT patient_hash_id FROM patient_pii_vault WHERE NOT is_synthetic;
DELETE FROM patient_invite_tokens WHERE used_by_patient IN (SELECT patient_hash_id FROM oldp);
DELETE FROM consents WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM notification_deliveries WHERE alert_id IN
  (SELECT c.alert_id FROM clinical_alerts_and_routing c JOIN oldp o ON c.patient_hash_id = o.patient_hash_id);
DELETE FROM critical_events_outbox WHERE payload->>'patient_hash_id' IN (SELECT patient_hash_id::text FROM oldp);
DELETE FROM multimodal_biomarkers_ts WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM patient_cluster_profiles WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM clinical_alerts_and_routing WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM adverse_events_and_risks WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM messages WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM patient_engagement WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM patient_diary_entries WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM medication_adherence_logs WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM patient_scale_assessments WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM therapy_plans_and_prescriptions WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM ehr_encounters_parsed WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM doctor_patient_bindings WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
DELETE FROM patient_pii_vault WHERE patient_hash_id IN (SELECT patient_hash_id FROM oldp);
COMMIT;
SELECT (SELECT count(*) FROM patient_pii_vault) AS vault,
       (SELECT count(*) FROM doctor_patient_bindings) AS bindings;
