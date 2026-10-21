#!/bin/bash
# deploy_debian.sh — выложить код на домашний Debian-сервер через Tailscale и прогнать проверки.
#
# Зачем: на Mac (arm64, clang) собирается только ветка -mcpu=native, а жюри увидит
# Linux/x86_64. Домашний сервер в Tailscale-кластере как раз x86_64 — на нём проверяем
# ветку -mavx2, gcc вместо clang и вообще всё, что зависит от платформы.
#
# На Mac tailscaled работает в userspace-режиме (без TUN), поэтому SSH до 100.x
# ходит через SOCKS5-прокси localhost:1055. Если у тебя TUN-режим — убери ProxyCommand.
set -euo pipefail

HOST="${DEBIAN_HOST:-100.104.45.89}"          # debian-server в Tailscale
REMOTE_DIR="${DEBIAN_DIR:-/root/rapira-clinical-compass}"
SOCKS="${TS_SOCKS:-127.0.0.1:1055}"

SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand='nc -X 5 -x ${SOCKS} %h %p' root@${HOST}"

echo "[*] Синхронизация кода на ${HOST}:${REMOTE_DIR} (через Tailscale)..."
rsync -az --delete \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyCommand='nc -X 5 -x ${SOCKS} %h %p'" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.so' \
  --exclude '*.pyc' --exclude '.DS_Store' --exclude 'postgres_data' --exclude 'redis_data' \
  "$(dirname "$0")/../" "root@${HOST}:${REMOTE_DIR}/"

echo "[*] Сборка C-ядра на Linux/x86_64 (ветка -mavx2):"
eval "${SSH_CMD} 'cd ${REMOTE_DIR} && python3 scripts/build_c_core.py'"

echo "[*] Smoke-тест нативного ядра (Метрика №1):"
eval "${SSH_CMD} 'cd ${REMOTE_DIR} && python3 -m tests.test_c_core_smoke'"

echo "[*] Тест 3 эталонных сценариев:"
eval "${SSH_CMD} 'cd ${REMOTE_DIR} && python3 -m backend.services.pipeline'"

echo "[OK] Debian-проверка прошла: Linux/x86_64 собирается и считает."
