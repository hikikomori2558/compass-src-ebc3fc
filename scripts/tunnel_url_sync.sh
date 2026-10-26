#!/usr/bin/env bash
# Самолечение trycloudflare-туннеля: после рестарта демона URL меняется.
# Скрипт: ловит новый URL из journalctl → обновляет .env (PATIENT_APP_URL) →
# перезапускает api → перегенерирует 3 QR (install/демо-хаб/лендинг).
# Ставится таймером каждую минуту (scripts/compass-tunnel-sync.timer).
set -euo pipefail
cd /root/rapira-clinical-compass

NEW=$(journalctl -u compass-tunnel --no-pager -n 200 2>/dev/null \
      | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)
[ -n "$NEW" ] || { echo "URL туннеля не найден"; exit 0; }

OLD=$(grep '^PATIENT_APP_URL=' .env 2>/dev/null | head -1 | sed 's/PATIENT_APP_URL=//;s|/static/pwa/||')
if [ "$NEW" = "$OLD" ]; then echo "URL не изменился: $NEW"; exit 0; fi

echo "URL изменился: $OLD -> $NEW"
sed -i "s|^PATIENT_APP_URL=.*|PATIENT_APP_URL=$NEW/static/pwa/|" .env
docker compose up -d api 2>&1 | tail -1
sleep 4

# перегенерация QR
docker compose exec -T api python3 - <<PYEOF
import json, urllib.request, qrcode
from qrcode.constants import ERROR_CORRECT_M
from pathlib import Path
BASE = "$NEW"
def req(path, data=None, token=None):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(BASE + "/api/v1" + path,
        data=json.dumps(data).encode() if data is not None else None, headers=h)
    return json.loads(urllib.request.urlopen(r, timeout=30).read())
t = req("/auth/login", {"login": "neuro.ivlanov", "password": "compass-demo-2026"})["access_token"]
ps = req("/auth/my-patients", token=t)
p1 = next((p for p in ps["patients"] if "С-001" in (p.get("label") or "")), ps["patients"][0])
dm = req("/auth/demo-patient-login", {"patient_hash": p1["patient_hash"]}, token=t)
out = Path("/root/rapira-clinical-compass/docs/screens/qr")
urls = {
    "1_install_ассистент_врача.png": BASE + "/static/compass/install.html",
    "2_демо-хаб_любой_пациент_ПОСТОЯННЫЙ.png": BASE + "/static/compass/demo-hub.html?key=compass-demo-public-2026",
    "3_лендинг_демо_стенда.png": BASE + "/static/compass/demo.html",
}
for name, url in urls.items():
    q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=3)
    q.add_data(url); q.make(fit=True)
    q.make_image(fill_color="#161616", back_color="white").save(out / name)
print("QR перегенерированы на", BASE)
PYEOF
git add docs/screens/qr .env 2>/dev/null || true
git commit -q -m "chore(tunnel): QR и PATIENT_APP_URL синхронизированы с новым URL туннеля" && git push -q origin main || true
echo "ГОТОВО: стенд доступен на $NEW"
