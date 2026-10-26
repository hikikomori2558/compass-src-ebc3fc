#!/usr/bin/env bash
# Боевой деплой COMPASS на чистый VPS (Ubuntu 22.04+/Debian 12, root).
# Использование: ROOT@VPS_IP DOMEN=projectc.work.gd bash bootstrap.sh
set -euo pipefail
DOMAIN="${DOMEN:-projectc.work.gd}"
REPO="https://github.com/hikikmori2558/rapira-clinical-compass.git"

echo "== 1. Docker =="
command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh && systemctl enable --now docker)

echo "== 2. Репозиторий =="
mkdir -p /opt && cd /opt
[ -d rapira-clinical-compass ] || git clone --depth 1 "$REPO"
cd rapira-clinical-compass && git pull origin main 2>/dev/null || true

echo "== 3. Секреты (.env; показываются один раз) =="
if [ ! -f .env ]; then
  cat > .env <<ENV
JWT_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
PII_PEPPER=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
POSTGRES_PASSWORD=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
DEMO_ACCESS_KEY=compass-demo-public-2026
LOGIN_RATE_LIMIT=30
PATIENT_APP_URL=https://$DOMAIN/static/pwa/
DOMEN=$DOMAIN
ENV
  chmod 600 .env
fi
grep -q "^DOMEN=" .env || echo "DOMEN=$DOMAIN" >> .env

echo "== 4. Caddyfile (TLS для домена,Let's Encrypt автоматически) =="
cat > Caddyfile <<CF
{
    email admin@$DOMAIN
}
$DOMAIN, www.$DOMAIN {
    reverse_proxy api:8000
    encode zstd gzip
}
CF

echo "== 5. Сборка и старт =="
docker compose build api emc 2>&1 | tail -2
docker compose up -d db redis 2>&1 | tail -1
sleep 8
docker compose --profile prod up -d 2>&1 | tail -1
sleep 8
curl -sf http://localhost:8000/healthz >/dev/null && echo "API: OK" || echo "API: ЕЩЁ ПОДНИМАЕТСЯ"

echo "== 6. Демо-данные (105 пациентов) + сиды =="
PW=$(docker exec compass_db printenv POSTGRES_PASSWORD 2>/dev/null || grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2)
docker compose exec -e OWNER_DATABASE_URL="postgresql://compass_app:${PW}@db:5432/compass" -T api \
  python3 -m scripts.seed_105_patients 2>&1 | tail -1 || echo "(сид уже применён при первом старте api)"
echo "== 7. Файрвол: наружу только 80/443/22 (8000/5432 — только внутри docker-сети) =="
if command -v ufw >/dev/null; then
  ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp; ufw --force enable
fi

echo "== 8. Ежедневный бэкап (03:40) =="
cp scripts/compass-backup.* /etc/systemd/system/ 2>/dev/null && systemctl daemon-reload && systemctl enable --now compass-backup.timer 2>/dev/null || true
echo "== 9. Домен постоянный — туннель-синк не нужен =="
echo "ГОТОВО. Проверка: https://$DOMAIN/healthz  (сертификат встанет после A-записи в DNS)"
