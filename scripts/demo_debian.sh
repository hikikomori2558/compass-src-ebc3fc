#!/bin/bash
# demo_debian.sh — поднять ПОЛНЫЙ демонстрационный стенд COMPASS-HYBRID на домашнем
# Debian-сервере (Tailscale-кластер) и прогнать ВСЕ проверки: от C-ядра до WS-шины
# и нагрузочного зонда. Одна команда = рабочая площадка для демо.
#
# Отличие от deploy_debian.sh: там — быстрая smoke-проверка Linux-сборки; здесь —
# полный стенд с docker-инфраструктурой, БД, тестами и весами моделей.
#
# Требования к ноде: docker + compose (проверено на debian-server), ssh root по ключу.
# С Mac tailscaled работает в userspace-режиме (без TUN) — SSH через SOCKS5 localhost:1055.
set -euo pipefail

HOST="${DEBIAN_HOST:-100.104.45.89}"
REMOTE_DIR="${DEBIAN_DIR:-/root/rapira-clinical-compass}"
SOCKS="${TS_SOCKS:-127.0.0.1:1055}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand="nc -X 5 -x ${SOCKS} %h %p")

run() { ssh "${SSH_OPTS[@]}" "root@${HOST}" "$@"; }

# Для rsync -e нужна СТРОКА с уже вложенными кавычками вокруг ProxyCommand:
# раскрытие массива через "..." развалило бы значение с пробелами на отдельные argv,
# и nc получил мусор вместо команды прокси (классическая ошибка квотинга).
RSYNC_RSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand='nc -X 5 -x ${SOCKS} %h %p'"

echo "=============================================================="
echo "[1/8] Синхронизация кода -> ${HOST}:${REMOTE_DIR}"
echo "=============================================================="
rsync -az --delete \
  -e "$RSYNC_RSH" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.so' \
  --exclude '*.pyc' --exclude '.DS_Store' --exclude 'postgres_data' --exclude 'redis_data' \
  --exclude 'clinical_data/hf_cache' --exclude 'models_cache' \
  "$(dirname "$0")/../" "root@${HOST}:${REMOTE_DIR}/"

echo "=============================================================="
echo "[2/8] Python-окружение на Debian"
echo "=============================================================="
run "cd ${REMOTE_DIR} && (python3 -m venv .venv 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-venv && python3 -m venv .venv)) && .venv/bin/pip -q install --upgrade pip && .venv/bin/pip -q install -r backend/requirements.txt huggingface_hub && .venv/bin/python3 -V"

echo "=============================================================="
echo "[3/8] Сборка C-ядра на Linux/x86_64 (ветка -mavx2) + смоук"
echo "=============================================================="
run "cd ${REMOTE_DIR} && .venv/bin/python3 scripts/build_c_core.py && .venv/bin/python3 -m tests.test_c_core_smoke | tail -3"

echo "=============================================================="
echo "[4/8] Автономные suite (правила / pii / лексер / безопасный разбор)"
echo "=============================================================="
run "cd ${REMOTE_DIR} && .venv/bin/python3 -m tests.test_domain_rules | tail -1 && .venv/bin/python3 -m tests.test_c_pii_scrubber | tail -1 && .venv/bin/python3 -m tests.test_c_lexer_ahocorasik | tail -1 && .venv/bin/python3 -m tests.test_safe_pipeline 2>&1 | tail -1"

echo "=============================================================="
echo "[5/8] Docker-стек: PostgreSQL 16 + Redis 7 + API (сборка образа на Linux)"
echo "=============================================================="
run "cd ${REMOTE_DIR} && docker compose up -d --build 2>&1 | tail -2"

echo "[*] Ждём /healthz (pg+redis+api)..."
run "for i in \$(seq 1 60); do curl -sf http://127.0.0.1:8000/healthz && break || sleep 2; done | tail -1"

echo "=============================================================="
echo "[6/8] БД: seed + RLS + аутентификация + WS-шина"
echo "=============================================================="
run "cd ${REMOTE_DIR} && .venv/bin/python3 -m backend.db.seed && .venv/bin/python3 -m tests.test_db_rls | tail -1 && .venv/bin/python3 -m tests.test_api_auth 2>&1 | tail -1 && .venv/bin/python3 -m tests.test_api_alerts_ws 2>&1 | tail -1"

echo "=============================================================="
echo "[7/8] Биоконтур: 100 виньеток + научные графики (headless)"
echo "=============================================================="
run "cd ${REMOTE_DIR} && MPLBACKEND=Agg .venv/bin/python3 -m benchmarks.ehr_nlp_benchmark | tail -6 && MPLBACKEND=Agg .venv/bin/python3 -m benchmarks.generate_scientific_plots | tail -1"

echo "=============================================================="
echo "[8/8] Нагрузочный зонд на 12 ядрах Debian"
echo "=============================================================="
run "cd ${REMOTE_DIR} && .venv/bin/python3 scripts/load_probe.py --scenario health --concurrency 128 --seconds 12 && .venv/bin/python3 scripts/load_probe.py --scenario analyze --concurrency 48 --seconds 12"

echo "=============================================================="
echo "[OK] Демо-стенд на ${HOST} поднят и полностью проверен:"
echo "     Swagger:  http://${HOST}:8000/docs   (из сети Tailscale)"
echo "=============================================================="
