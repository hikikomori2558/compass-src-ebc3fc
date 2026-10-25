#!/bin/bash
# api_smoke.sh — живое тыканье в API поднятого стенда (вместо ручного кликанья).
#
# Проверяет то, что видит жюри: 401 без токена, вход, три демо-сценария ЭМК,
# PK/фенотипы АРМ, реестр с градациями доказательности, hot-reload шкал.
# БД не нужна: JWT минтится локально с тем же JWT_SECRET из окружения (.env).
#
# Запуск: scripts/api_smoke.sh [BASE_URL]     (по умолчанию http://127.0.0.1:8000)
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-http://127.0.0.1:8000}"
PY="python3"; [ -x .venv/bin/python ] && PY=".venv/bin/python"

echo "== 0. healthz"
HEALTH=$(curl -sf "$BASE/healthz")
echo "   $HEALTH"

TOKEN=$($PY -c "from backend.services.auth import mint_access; print(mint_access('smoke-doctor','DOCTOR_PSYCHIATRIST'))")
ADMIN=$($PY -c "from backend.services.auth import mint_access; print(mint_access('smoke-admin','ADMIN_METHODOLOGIST'))")

echo "== 1. аноним без токена -> ожидаем 401"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/compass-bio/scales-and-trials-registry")
[ "$CODE" = "401" ] && echo "   OK: 401" || { echo "   FAIL: $CODE"; exit 1; }

echo "== 2. /auth/me с токеном"
curl -sf "$BASE/api/v1/auth/me" -H "Authorization: Bearer $TOKEN" | grep -q '"is_doctor":true' && echo "   OK: врач распознан"

echo "== 3. Сценарий 1: пост-COVID водитель, In-Label"
$PY - "$BASE" "$TOKEN" "$ADMIN" <<'EOF'
import json, sys, urllib.request
base, token, admin = sys.argv[1], sys.argv[2], sys.argv[3]
def post(path, payload, tok=token):
    req = urllib.request.Request(base + path, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
    return json.load(urllib.request.urlopen(req))

r = post("/api/v1/compass-bio/ehr/analyze-and-route", {"raw_ehr_text":
    "Осмотр невролога. Мужчина, 44 г. Код МКБ: F43.2. После COVID-19. Тревога, инсомния. "
    "Принимает метопролол. ИМАО отрицает. Лаб: креат. 94, печень в норме, Чайлд-Пью: норма. "
    "Шкалы: ГТР-7 14б, PHQ9 8б (п9=0), CSSRS=0."})
assert r["overall_status"] in ("GREEN_IN_LABEL", "AMBER_REVIEW", "AMBER_DATA_REQUIRED"), r["overall_status"]
print("   OK:", r["overall_status"], "| маршрут:", r["compass_decision"]["routing_and_alerting"]["routing_category"],
      "| xai:", bool(r.get("xai_gost_59921_shap")))

print("== 4. Сценарий 2: полипрагмазия, DDI CYP2D6/3A4")
r2 = post("/api/v1/cdss/evaluate-encounter", {
    "ehr_note_text": "Пациентка принимает Авиандр, пароксетин и кларитромицин. Водит авто.",
    "cyp2d6_activity_score": 0.12})
pk = r2["poppk_rk45"]
assert pk["risk_classification"] in ("TOXIC_OVERDOSE_HARD_STOP", "SUPRATHERAPEUTIC_OVEREXPOSURE_WARNING"), pk
pheno = [p["code"] for p in r2.get("phenotype_guidance", [])]
assert "GAD_ACTIVE_DRIVER" in pheno, pheno
print("   OK:", r2["routing_category"], "| Cmax %.0f" % pk["c_max_ng_ml"], "| фенотипы:", pheno)

print("== 5. Сценарий 3: суицидальный триггер -> ОБЯЗАТЕЛЬНО")
r3 = post("/api/v1/compass-bio/ehr/analyze-and-route", {"raw_ehr_text":
    "Психический статус. Женщина, 29 лет. F41.1. Тревога, высказывает суицидальные мысли. "
    "PHQ9 23 (п9=2), CSSRS=4, ГТР-7 18. Креат. 70, печень в норме. Эпилепсии нет, лактазы нет, ИМАО не принимала."})
cat = r3["compass_decision"]["routing_and_alerting"]["routing_category"]
assert cat == "MANDATORY_PSYCHIATRIST", cat
print("   OK: MANDATORY_PSYCHIATRIST")

print("== 6. Реестр: доказательность + фенотипы + визиты")
req = urllib.request.Request(base + "/api/v1/compass-bio/scales-and-trials-registry",
                             headers={"Authorization": "Bearer " + token})
reg = json.load(urllib.request.urlopen(req))
assert "anxiolytic" in reg.get("evidence_grades", {}) and len(reg.get("patient_phenotypes", {})) == 6
print("   OK: градации", len(reg["evidence_grades"]) - 1, "| фенотипов 6 | визиты:", len(reg.get("monitoring_schedule", {})))

print("== 7. Hot-reload шкалы (только ADMIN_METHODOLOGIST, Метрика №10)")
r7 = post("/api/v1/compass-bio/admin/hot-reload-scale-plugin",
          {"scale_code": "SMOKE-7", "title_ru": "Смоук-шкала"}, tok=admin)
assert r7["status"] == "PLUGIN_REGISTERED_HOT_RELOAD", r7
print("   OK: шкала добавлена на лету, всего:", r7["total_scales_in_registry"])

print("\nВСЕ ПРОВЕРКИ ЖИВОГО API ПРОЙДЕНЫ.")
EOF
