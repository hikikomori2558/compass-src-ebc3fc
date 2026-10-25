#!/usr/bin/env bash
# Резервное копирование COMPASS (метрика 7: отказоустойчивость и восстановление данных).
#   * pg_dump -Fc (custom format) — восстановление одной командой, RTO ~2–5 минут;
#   * храним последние 7 копий (RPO ≤ 24 ч при ежедневном запуске; критические
#     события дублируются в transactional outbox и не теряются между копиями);
#   * systemd-timer: см. scripts/compass-backup.timer (03:40 ежедневно).
set -euo pipefail
BACKUP_DIR="${BACKUP_DIR:-/root/compass_backups}"
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"
docker exec compass_db pg_dump -U compass_app -d compass -Fc \
  -f "/tmp/compass_${STAMP}.dump"
docker cp "compass_db:/tmp/compass_${STAMP}.dump" "$BACKUP_DIR/compass_${STAMP}.dump"
docker exec compass_db rm -f "/tmp/compass_${STAMP}.dump"
ls -1t "$BACKUP_DIR"/compass_*.dump | tail -n +8 | xargs -r rm -f
echo "OK: $BACKUP_DIR/compass_${STAMP}.dump ($(du -h "$BACKUP_DIR/compass_${STAMP}.dump" | cut -f1))"
