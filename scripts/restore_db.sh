#!/usr/bin/env bash
# Восстановление из копии (учения 1 раз в квартал — порядок записан в COMPLIANCE_MATRIX):
#   ./scripts/restore_db.sh /root/compass_backups/compass_YYYYmmdd_HHMMSS.dump
# Восстанавливает В ОДНУ КОМАНДУ. Для учений добавьте RESTORE_TO_SCRATCH=1 —
# тогда данные лягут в отдельную БД compass_restore_check и боевая не тронется.
set -euo pipefail
DUMP="${1:?путь к .dump}"
SCRATCH="${RESTORE_TO_SCRATCH:-0}"
if [ "$SCRATCH" = "1" ]; then
  docker exec compass_db psql -U compass_app -d postgres -c "DROP DATABASE IF EXISTS compass_restore_check;"
  docker exec compass_db psql -U compass_app -d postgres -c "CREATE DATABASE compass_restore_check;"
  docker cp "$DUMP" "compass_db:/tmp/restore.dump"
  docker exec compass_db pg_restore -U compass_app -d compass_restore_check --no-owner /tmp/restore.dump
  ROWS=$(docker exec compass_db psql -U compass_app -d compass_restore_check -t -c \
    "SELECT (SELECT count(*) FROM patient_pii_vault) || ' пациентов, ' || (SELECT count(*) FROM patient_scale_assessments) || ' шкал'")
  echo "УЧЁТ ВОССТАНОВЛЕНИЯ: $ROWS (в scratch-БД; боевая не тронута)"
  docker exec compass_db psql -U compass_app -d postgres -c "DROP DATABASE compass_restore_check;"
else
  docker cp "$DUMP" "compass_db:/tmp/restore.dump"
  docker exec compass_db psql -U compass_app -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='compass' AND pid <> pg_backend_pid();"
  docker exec compass_db psql -U compass_app -d postgres -c "DROP DATABASE compass;"
  docker exec compass_db psql -U compass_app -d postgres -c "CREATE DATABASE compass;"
  docker exec compass_db pg_restore -U compass_app -d compass --no-owner /tmp/restore.dump
  docker exec compass_db rm -f /tmp/restore.dump
  echo "ВОССТАНОВЛЕНО: compass из $DUMP"
fi
