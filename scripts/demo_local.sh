#!/bin/bash
# demo_local.sh — подъём ПОЛНОГО демо-стенда ОДНОЙ командой.
#
# Заточен под типовую схему команды: разработка на ВИРТУАЛКЕ в локальной сети,
# а смотрят демо с хоста (браузер) и с телефона (PWA). Поэтому:
#   - все URL печатаются с LAN-IP виртуалки (127.0.0.1 с хоста не работает!);
#   - QR-приглашение пациента сразу ведёт на http://<LAN-IP>:5501/;
#   - фронт сам находит API по hostname страницы (clinical_engines_client.js).
#
# Сценарий показа для руководителя: docs/DEMO_SCRIPT.md
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }

# LAN-IP виртуалки: первый адрес из hostname -I (Linux), fallback для macOS
LAN_IP="${LAN_IP:-$(hostname -I 2>/dev/null | awk '{print $1}' || true)}"
LAN_IP="${LAN_IP:-$(ipconfig getifaddr en0 2>/dev/null || true)}"
if [ -z "${LAN_IP:-}" ]; then LAN_IP="<LAN-IP-виртуалки>"; fi

say "Стенд на виртуалке: LAN-IP = ${LAN_IP} (открывать с хоста и телефона по нему)"

say "1/6 · Python-окружение (venv + зависимости)"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r backend/requirements.txt

say "2/6 · Сборка C-ядра (libcompass_core.so)"
python3 scripts/build_c_core.py

# QR пациента должен вести на адрес, открывающийся с телефона -> прокидываем в API
export PATIENT_APP_URL="http://${LAN_IP}:5501/"

if command -v docker >/dev/null 2>&1; then
  say "3/6 · Docker: PostgreSQL 16 + Redis 7 + API (PATIENT_APP_URL=${PATIENT_APP_URL})"
  docker compose up -d --build
  say "4/6 · Ждём /healthz (до 2 минут)..."
  ok=""
  for _ in $(seq 1 60); do
    if curl -sf "http://${LAN_IP}:8000/healthz" >/dev/null 2>&1 || curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ -z "$ok" ]; then echo "[!] API не поднялся. Логи: docker compose logs api"; exit 1; fi
  say "5/6 · Демо-данные БД (врачи, пациенты, шкалы)"
  python3 -m backend.db.seed
else
  # Нативный Postgres без докера (Debian): если 5432 отвечает — работаем в ПОЛНОМ режиме.
  if (echo >/dev/tcp/127.0.0.1/5432) 2>/dev/null; then
    say "3/6 · Docker не найден, но найден нативный Postgres:5432 — полный режим"
    export DATABASE_URL="${DATABASE_URL:-postgresql://compass_api:compass_api_demo@localhost:5432/compass}"
    export ENABLE_DB=1
    ( HOST=0.0.0.0 python3 -m backend.main >/tmp/compass_api.log 2>&1 & )
    sleep 3
    say "4/6 · Демо-данные (seed) при необходимости"
    OWNER_DATABASE_URL="${OWNER_DATABASE_URL:-postgresql://compass_app:compass_demo@localhost:5432/compass}" \
      python3 -m backend.db.seed 2>&1 | tail -1 || echo "[!] seed пропущен (возможно, уже залит)"
  else
    say "3/6 · Docker не найден: поднимаю API без БД (HOST=0.0.0.0 — видно с хоста)"
    say "    (вход врача и WS-алерты будут недоступны — АРМ покажет офлайн-режим с бейджем)"
    ( HOST=0.0.0.0 python3 -m backend.main >/tmp/compass_api.log 2>&1 & )
    sleep 3
  fi
fi

say "6/6 · Фронтенды (статика, bind 0.0.0.0 — видны с хоста и телефона)"
pkill -f "http.server 5500" 2>/dev/null || true
pkill -f "http.server 5501" 2>/dev/null || true
sleep 1
python3 -m http.server 5500 --bind 0.0.0.0 -d frontend_doctor  >/tmp/compass_5500.log 2>&1 &
python3 -m http.server 5501 --bind 0.0.0.0 -d frontend_patient >/tmp/compass_5501.log 2>&1 &
sleep 1

MODE=$(curl -s "http://${LAN_IP}:8000/healthz" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('ПОЛНЫЙ (БД+алерты)' if d['status']=='ok' else 'DEGRADED (офлайн-фоллбэк)')" 2>/dev/null || echo "?")
cat <<EOF

=================== ДЕМО-СТЕНД ПОДНЯТ ===================
  Режим: ${MODE}
  С ХОСТА (браузер):
    АРМ врача:      http://${LAN_IP}:5500
    PWA пациента:   http://${LAN_IP}:5501
    Swagger API:    http://${LAN_IP}:8000/docs
  С ТЕЛЕФОНА (та же сеть): http://${LAN_IP}:5501
  Вход врача:      neuro.ivlanov / compass-demo-2026
  Врач-психиатр:  psych.petrova / compass-demo-2026 (для смены роли)
  Сценарий показа: docs/DEMO_SCRIPT.md

  Если с хоста НЕ открывается — на виртуалке закрыты порты:
    sudo ufw allow 5500,5501,8000/tcp   (или проверь NAT/bridged сеть виртуалки)
  Остановка: pkill -f 'http.server 55'; docker compose down
==========================================================
EOF
