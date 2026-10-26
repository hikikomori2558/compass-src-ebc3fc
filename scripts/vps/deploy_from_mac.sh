#!/usr/bin/env bash
# Деплой с Mac на боевой VPS: свежий дамп стенда + репо + bootstrap + restore.
# Использование: bash scripts/vps/deploy_from_mac.sh   (ключ ~/.ssh/compass_vps)
set -euo pipefail
KEY="$HOME/.ssh/compass_vps"
VPS="root@168.113.173.61"
LAPTOP="root@100.104.45.89"

echo "== 1. свежий дамп с текущего стенда (ноутбук) =="
ssh -o BatchMode=yes -o ConnectTimeout=20 \
    -o ProxyCommand="nc -X 5 -x 127.0.0.1:1055 %h %p" "$LAPTOP" \
    'cd /root/rapira-clinical-compass && BACKUP_DIR=/root/compass_backups bash scripts/backup_db.sh' | tail -1
DUMP=$(ssh -o BatchMode=yes -o ProxyCommand="nc -X 5 -x 127.0.0.1:1055 %h %p" "$LAPTOP" \
    'ls -1t /root/compass_backups/compass_*.dump | head -1')
scp -o BatchMode=yes -o ProxyCommand="nc -X 5 -x 127.0.0.1:1055 %h %p" \
    "$LAPTOP:$DUMP" /tmp/compass_latest.dump
echo "дамп: $(du -h /tmp/compass_latest.dump | cut -f1)"

echo "== 2. репо на VPS =="
ssh -i "$KEY" -o PasswordAuthentication=no "$VPS" \
    "mkdir -p /opt/rapira-clinical-compass /root/compass_backups"
tar czf - --exclude '.venv' --exclude 'node_modules' --exclude '.git' \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' --no-xattrs \
    -C /Users/hikikmori/rapira-clinical-compass . | \
  ssh -i "$KEY" -o PasswordAuthentication=no "$VPS" \
    "tar xzf - -C /opt/rapira-clinical-compass"
echo "репо на VPS"

echo "== 3. дамп на VPS =="
scp -i "$KEY" -o PasswordAuthentication=no /tmp/compass_latest.dump \
    "$VPS:/root/compass_backups/compass_latest.dump"
echo "дамп доставлен"

echo "== 4. bootstrap (docker, секреты, стек, Caddy, файрвол) =="
ssh -i "$KEY" -o PasswordAuthentication=no "$VPS" \
    "cd /opt/rapira-clinical-compass && bash scripts/vps/bootstrap.sh" 2>&1 | tail -10

echo "== 5. восстановление БД (105 пациентов, вся ретроспектива) =="
ssh -i "$KEY" -o PasswordAuthentication=no "$VPS" \
    "cd /opt/rapira-clinical-compass && docker compose up -d db"
sleep 8
ssh -i "$KEY" -o PasswordAuthentication=no "$VPS" \
    "docker cp /root/compass_backups/compass_latest.dump compass_db:/tmp/rest.dump && \
     docker exec compass_db pg_restore -U compass_app -d compass --clean --if-exists --no-owner /tmp/rest.dump >/dev/null 2>&1; \
     docker exec compass_db rm -f /tmp/rest.dump; echo restore-done"
sleep 2
ssh -i "$KEY" -o PasswordAuthentication=no "$VPS" \
    "cd /opt/rapira-clinical-compass && docker compose restart api emc >/dev/null 2>&1; sleep 5; \
     curl -s -o /dev/null -w 'healthz: %{http_code}\n' http://localhost:8000/healthz"
echo "== ДЕПЛОЙ ЗАВЕРШЁН =="
