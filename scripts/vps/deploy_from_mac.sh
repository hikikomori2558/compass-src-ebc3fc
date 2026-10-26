#!/usr/bin/env bash
# Деплой с моего Mac на боевой VPS: репо + свежий дамп БД + bootstrap.
set -euo pipefail
VPS=root@168.113.173.61
KEY=~/.ssh/compass_vps
SSH="ssh -i $KEY -o PasswordAuthentication=no"
SCP="scp -i $KEY -o PasswordAuthentication=no"

echo "== 1. свежий дамп с текущего стенда (ноутбук) =="
ssh -o BatchMode=yes -o ConnectTimeout=20 -o ProxyCommand="nc -X 5 -x 127.0.0.1:1055 %h %p" root@100.104.45.89 \
  'cd /root/rapira-clinical-compass && BACKUP_DIR=/root/compass_backups bash scripts/backup_db.sh' | tail -1
DUMP=$(ssh -o BatchMode=yes -o ProxyCommand="nc -X 5 -x 127.0.0.1:1055 %h %p" root@100.104.45.89 'ls -1t /root/compass_backups/compass_*.dump | head -1')
scp -o BatchMode=yes -o ProxyCommand="nc -X 5 -x 127.0.0.1:1055 %h %p" root@100.104.45.89:"$DUMP" /tmp/compass_latest.dump
echo "дамп: $(du -h /tmp/compass_latest.dump | cut -f1)"

echo "== 2. репо и дамп на VPS =="
$SSH 'mkdir -p /opt /root/compass_backups'
rsync -az --delete -e "$SSH" --exclude .venv --exclude node_modules --exclude ".git" \
  /Users/hikikmori/rapira-clinical-compass/ $VPS:/opt/rapira-clinical-compass/
$SCP /tmp/compass_latest.dump $VPS:/root/compass_backups/compass_latest.dump

echo "== 3. bootstrap на VPS (docker, секреты, стек, Caddy, файрвол) =="
$SSH 'cd /opt/rapira-clinical-compass && bash scripts/vps/bootstrap.sh' 2>&1 | tail -12

echo "== 4. восстановление БД из дампа (со всеми 105 пациентами) =="
$SSH 'docker compose -f /opt/rapira-clinical-compass/docker-compose.yaml up -d db 2>/dev/null || docker compose -f /opt/rapira-clinical-compass/docker-compose.yml up -d db'
sleep 8
$SSH 'docker cp /root/compass_backups/compass_latest.dump compass_db:/tmp/rest.dump && \
      docker exec compass_db pg_restore -U compass_app -d compass --clean --if-exists --no-owner /tmp/rest.dump 2>&1 | grep -v "already exists" | tail -2; \
      docker exec compass_db rm -f /tmp/rest.dump; echo restore-done'
sleep 2
$SSH 'docker compose -f /opt/rapira-clinical-compass/docker-compose.yml restart api emc 2>&1 | tail -1; sleep 4; curl -s -o /dev/null -w "healthz: %{http_code}\n" http://localhost:8000/healthz'
echo "== ДЕПЛОЙ ЗАВЕРШЁН =="
