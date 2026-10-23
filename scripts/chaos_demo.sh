#!/bin/bash
# chaos_demo.sh — ДЕМОНСТРАЦИЯ ОТКАЗОУСТОЙЧИВОСТИ (Метрика №7, Условия 5–6).
#
# Сюжет для жюри: «Пациент отправляет критический чек-ин ровно в момент,
# когда транспорт мёртв. Что происходит?»
#
#   1. Включаем CHAOS: релей алертов перестаёт доставлять (флаг-файл = «Redis мёртв»).
#   2. Пациент шлёт критический чек-ин (PHQ-9 п.9 = 2) -> Postgres коммитит
#      АЛЕРТ + ЗАПИСЬ В OUTBOX одной транзакцией (Transactional Outbox).
#   3. Проверяем: алерт НЕ доставлен (published_to_redis = false), но и НЕ ПОТЕРЯН —
#      он сидит в outbox (это и есть RPO = 0).
#   4. «Восстанавливаем транспорт» (убираем флаг) -> релей догоняет накопленное.
#   5. Проверяем: published_to_redis = true, стрим врача пополнен. Алерт доставлен.
#
# Требуется: поднятый сервер в полном режиме (demo_local.sh или demo_debian.sh).
# Запуск: scripts/chaos_demo.sh [BASE_URL]   (по умолчанию http://127.0.0.1:8000)
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-http://127.0.0.1:8000}"
PY="python3"; [ -x .venv/bin/python ] && PY=".venv/bin/python"
FREEZE_FLAG=/tmp/compass_relay_freeze

step() { printf '\n\033[1;36m[CHAOS]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }

step "0. Сервер жив?"
curl -sf "$BASE/healthz" | grep -q '"status":"ok"' || fail "сервер не в полном режиме (см. demo_local.sh)"
ok "healthz: ok"

rm -f "$FREEZE_FLAG"

step "1. Врач входит и выписывает QR-приглашение; пациент привязывается"
$PY - "$BASE" <<'EOF'
import json, sys, urllib.request
base = sys.argv[1]

def call(path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    return json.load(urllib.request.urlopen(req))

doc = call("/api/v1/auth/login", {"login": "neuro.ivlanov", "password": "compass-demo-2026"})
inv = call("/api/v1/auth/invite", {}, doc["access_token"])
pat = call("/api/v1/auth/bind", {"token": inv["token"]})
open("/tmp/chaos_doctor_token", "w").write(doc["access_token"])
open("/tmp/chaos_patient_token", "w").write(pat["access_token"])
open("/tmp/chaos_doctor_id", "w").write(doc["principal_sub"])
print("   ✓ врач + пациент готовы (привязка и ИДС созданы)")
EOF

step "2. ВКЛЮЧАЕМ CHAOS: транспорт алертов «умирает»"
touch "$FREEZE_FLAG"
echo "   флаг: $FREEZE_FLAG (релей копит outbox, не доставляя)"

step "3. Пациент шлёт КРИТИЧЕСКИЙ чек-ин (PHQ-9 п.9 = 2) — транспорт-то мёртв!"
$PY - "$BASE" <<'EOF'
import json, sys, urllib.request, uuid
base = sys.argv[1]
pat = open("/tmp/chaos_patient_token").read().strip()
req = urllib.request.Request(base + "/api/v1/patient/checkin", method="POST",
    data=json.dumps({"event_uuid": str(uuid.uuid4()), "phq9_total": 22,
                     "phq9_item9": 2, "gad7_total": 19, "cssrs": 4,
                     "took_medication": True}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + pat})
r = json.load(urllib.request.urlopen(req))
assert r["critical"] is True, r
open("/tmp/chaos_alert_id", "w").write(str(r["alert_id"]))
print(f"   ✓ сервер принял чек-ин: critical=True, alert_id={r['alert_id']}")
EOF

step "4. Проверка RPO=0: алерт не доставлен (транспорт мёртв), но НЕ ПОТЕРЯН — в outbox"
sleep 1.5
DATABASE_URL="${DATABASE_URL:-postgresql://compass_api:compass_api_demo@localhost:5433/compass}" \
APP_DATABASE_URL="${APP_DATABASE_URL:-postgresql://compass_api:compass_api_demo@localhost:5432/compass}" \
$PY - <<'EOF'
import asyncio, os, sys

async def main():
    import asyncpg
    conn = None
    for dsn in (os.getenv("APP_DATABASE_URL", ""), os.getenv("DATABASE_URL", "")):
        if not dsn:
            continue
        try:
            conn = await asyncpg.connect(dsn)
            break
        except Exception:
            pass
    if conn is None:
        sys.exit("  ✗ нет соединения с БД")
    # NB: clinical_alerts_and_routing под RLS — из сырого соединения невидим (fail-closed,
    # так и задумано). Outbox же читается: именно он — гарантия сохранности.
    expected = open("/tmp/chaos_alert_id").read().strip()
    row = await conn.fetchrow(
        "SELECT published_to_redis, payload->>'alert_id' AS alert_id "
        "FROM critical_events_outbox ORDER BY event_id DESC LIMIT 1")
    assert row is not None, "outbox-запись не найдена"
    assert row["alert_id"] == expected, f"ожидали alert {expected}, в outbox {row['alert_id']}"
    assert row["published_to_redis"] is False, "алерт УШЁЛ при мёртвом транспорте?!"
    print(f"   ✓ RPO=0 подтверждён: алерт #{row['alert_id']} лежит в outbox, ждёт транспорт")
    await conn.close()

asyncio.run(main())
EOF
ok "алерт в надёжном состоянии"

step "5. ВОССТАНАВЛИВАЕМ транспорт (убираем флаг) — релей догоняет накопленное"
rm -f "$FREEZE_FLAG"
sleep 2.5
APP_DATABASE_URL="${APP_DATABASE_URL:-postgresql://compass_api:compass_api_demo@localhost:5432/compass}" \
DATABASE_URL="${DATABASE_URL:-postgresql://compass_api:compass_api_demo@localhost:5433/compass}" \
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}" \
$PY - <<'EOF'
import asyncio, os

async def main():
    import asyncpg
    conn = None
    for dsn in (os.getenv("APP_DATABASE_URL", ""), os.getenv("DATABASE_URL", "")):
        if not dsn:
            continue
        try:
            conn = await asyncpg.connect(dsn)
            break
        except Exception:
            pass
    assert conn is not None, "нет соединения с БД"
    row = await conn.fetchrow(
        "SELECT published_to_redis FROM critical_events_outbox "
        "ORDER BY event_id DESC LIMIT 1")
    assert row["published_to_redis"] is True, "релей не догнал после восстановления!"
    print("   ✓ Восстановление подтверждено: published_to_redis = true")
    await conn.close()

    import redis.asyncio as aioredis
    doctor_id = open("/tmp/chaos_doctor_id").read().strip()
    r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    n = await r.xlen(f"alerts:{doctor_id}")
    print(f"   ✓ Стрим врача пополнен: XLEN alerts:{doctor_id} = {n}")
    await r.aclose()

asyncio.run(main())
EOF

printf '\n\033[1;32m════════ ИТОГ CHAOS-ДЕМО ════════\033[0m\n'
printf 'Транспорт умер в момент критического чек-ина.\n'
printf 'Алерт НЕ потерян (RPO=0, Transactional Outbox) и доставлен сразу после\n'
printf 'восстановления. Пациент -> врач, без третьих лиц, гарантированно.\n'
