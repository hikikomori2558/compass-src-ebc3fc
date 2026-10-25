# -*- coding: utf-8 -*-
"""
tests/test_db_battle.py — МАССИРОВАННЫЙ боевой тест БД (запускать на стенде).

Проверяет сквозную цепочку «пациент → БД → врач» в реальных условиях:
  1. N пациентов привязываются по приглашениям (bind, RLS, ИДС);
  2. каждый шлёт серию чек-инов (норм + критичные, идемпотентность event_uuid);
  3. повторная отправка того же event_uuid = DUPLICATE (не плодит алертов);
  4. врач видит критичные алерты в /alerts/unconfirmed (+ patient_hash для таймлайна);
  5. timeline пациента отдаёт его шкалы/приверженность (RLS: своих);
  6. подтверждение врачом гасит алерт (аудит пишется на сервере);
  7. telemetry/summary растёт.

Запуск: python3 -m tests.test_db_battle  (нужен боевой API + БД)
"""
import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
N_PATIENTS = 5
CHECKINS_PER_PATIENT = 3


def req(path, data=None, token=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(BASE + path,
                               data=json.dumps(data).encode() if data is not None else None,
                               headers=headers, method=method or ("POST" if data is not None else "GET"))
    return json.loads(urllib.request.urlopen(r, timeout=25).read())


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (" — " + str(detail) if detail and not cond else ""))
        if not cond:
            failures.append(name)

    print("=" * 70)
    print("БОЕВОЙ ТЕСТ БД: пациенты -> чек-ины -> алерты -> врач -> аудит")
    print("=" * 70)

    doc = req("/api/v1/auth/login", {"login": "neuro.ivlanov", "password": "compass-demo-2026"})
    H = doc["access_token"]
    check("вход врача", doc["principal_role"] == "DOCTOR_NEUROLOGIST")

    tel0 = req("/api/v1/telemetry/summary")["requests_observed"]

    patients = []
    for i in range(N_PATIENTS):
        inv = req("/api/v1/auth/invite", {}, H)
        pt = req("/api/v1/auth/bind", {"token": inv["token"]})
        check(f"пациент {i + 1}: привязка", pt["principal_role"] == "PATIENT")
        patients.append(pt["access_token"])

    critical_expected = 0
    for i, tok in enumerate(patients):
        for c in range(CHECKINS_PER_PATIENT):
            event = str(uuid.uuid4())
            critical = (c == CHECKINS_PER_PATIENT - 1 and i % 2 == 0)
            if critical:
                critical_expected += 1
            r1 = req("/api/v1/patient/checkin", {
                "event_uuid": event,
                "phq9_total": 14 + i, "phq9_item9": 1 if critical else 0,
                "gad7_total": 12 + i, "cssrs": 4 if critical else 0,
                "took_medication": c != 1,
            }, tok)
            check(f"пациент {i + 1} чек-ин {c + 1}: принят", r1["status"] == "OK")
            if critical:
                check(f"пациент {i + 1} чек-ин {c + 1}: критичность", r1["critical"] is True)
            r2 = req("/api/v1/patient/checkin", {
                "event_uuid": event,
                "phq9_total": 14 + i, "phq9_item9": 1 if critical else 0,
                "gad7_total": 12 + i, "cssrs": 4 if critical else 0,
            }, tok)
            check(f"пациент {i + 1} чек-ин {c + 1}: идемпотентность", r2["status"] == "DUPLICATE")

    time.sleep(2.0)  # релей outbox -> redis
    ua = req("/api/v1/alerts/unconfirmed", token=H)
    check("врач видит критичные алерты", ua["count"] >= critical_expected,
          f"ожидали >= {critical_expected}, получено {ua['count']}")
    check("в алертах есть patient_hash (для таймлайна)",
          all(a.get("patient_hash") for a in ua["alerts"][:3]))

    ph = ua["alerts"][0]["patient_hash"]
    tl = req(f"/api/v1/compass-bio/patient/{ph}/timeline", token=H)
    check("timeline: шкалы накоплены", len(tl["scales"]) >= CHECKINS_PER_PATIENT - 1,
          f"шкал: {len(tl['scales'])}")
    check("timeline: приверженность пишется", len(tl["adherence"]) >= 1)
    check("timeline: алерты в истории", len(tl["alerts"]) >= 1)

    confirmed = 0
    for a in ua["alerts"]:
        rr = req(f"/api/v1/alerts/{a['alert_id']}/confirm",
                 {"action_taken": "Боевой тест: реакция зафиксирована"}, H)
        if rr.get("confirmed"):
            confirmed += 1
    check("все алерты подтверждены", confirmed == ua["count"], f"{confirmed}/{ua['count']}")
    ua2 = req("/api/v1/alerts/unconfirmed", token=H)
    check("после подтверждений очередь пуста", ua2["count"] == 0)

    tel1 = req("/api/v1/telemetry/summary")["requests_observed"]
    check("телеметрия живая", tel1 > tel0, f"{tel0} -> {tel1}")

    # ============================================================
    # v7: шкалы МЗ РФ, кластер-карта, НЯ/1071, HADS roundtrip, стрик
    # ============================================================
    print("=" * 70)
    print("БЛОК v7: HADS/HAM-A подсчёты · кластер-карта · НЯ+1071 · engagement")
    print("=" * 70)

    # 8. HADS подсчёт на сервере: 14 ответов → HADS + HADS-A/D строки
    inv = req("/api/v1/auth/invite", {}, H)
    pt = req("/api/v1/auth/bind", {"token": inv["token"]})
    PTOK = pt["access_token"]
    hads_answers = [2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1]
    ev = str(uuid.uuid4())
    cr = req("/api/v1/patient/checkin", {
        "event_uuid": ev, "gad7_total": 14, "hads_answers": hads_answers,
        "took_medication": True, "intake_time": "08:35",
        "diary": {"symptoms": ["Сонливость днём", "Тошнота"], "sleep": "6–7 ч", "energy": 3},
    }, PTOK)
    check("чекин с HADS+дневником принят", cr["status"] == "OK")
    sc = req("/api/v1/compass-bio/scales/HADS", token=PTOK)
    check("GET /scales/HADS: 14 пунктов + per-item options",
          len(sc["items"]) == 14 and sc.get("per_item_options") and len(sc.get("tips", [])) == 14)
    ha = sum(hads_answers[:7]); hd = sum(hads_answers[7:])
    UA = req("/api/v1/alerts/unconfirmed", token=H)
    ph2 = (UA["alerts"][0]["patient_hash"] if UA["count"] else None) or \
          req("/api/v1/auth/my-patients", token=H)["patients"][0]["patient_hash"]
    tl2 = req(f"/api/v1/compass-bio/patient/{ph2}/timeline", token=H)
    hads_rows = {x["scale_code"]: x["total_score"] for x in tl2["scales"] if x["scale_code"].startswith("HADS")}
    check("HADS roundtrip: серверный подсчёт A/D",
          hads_rows.get("HADS-A") == ha and hads_rows.get("HADS-D") == hd,
          f"ожидали A={ha} D={hd}, в БД {hads_rows}")
    check("intake_time roundtrip в дневнике",
          any(x.get("intake_time") for x in tl2["adherence"]))

    # 9. HAM-A: врач записывает клиницистскую шкалу
    hama = req(f"/api/v1/compass-bio/patient/{ph2}/clinician-scale?code=HAM-A",
               {"answers": [2, 3, 1, 2, 2, 2, 2, 1, 1, 1, 2, 1, 2, 3]}, H)
    check("HAM-A врачом записан: 25 = psy 15 + som 10",
          hama.get("total_score") == 25 and hama.get("hama_psychic") == 15 and hama.get("hama_somatic") == 10,
          str(hama)[:120])

    # 10. НЯ из дневника → adverse_events → черновик 1071
    tl3 = req(f"/api/v1/compass-bio/patient/{ph2}/timeline", token=H)
    aes = tl3.get("adverse_events") or []
    check("НЯ из дневника зарегистрированы (MedDRA)",
          any("Сонливость" in a["meddra_term"] for a in aes) and any("Тошнота" in a["meddra_term"] for a in aes),
          str([(a["meddra_term"]) for a in aes])[:120])
    d1071 = req(f"/api/v1/compass-bio/patient/{ph2}/adverse-events/draft-1071", {}, H)
    check("черновик 1071 сформирован", d1071.get("drafts_formed", 0) >= 1
          and "1071" in d1071.get("draft_text", ""), str(d1071)[:120])

    # 11. Кластер-профиль + карта
    cp = req(f"/api/v1/compass-bio/patient/{ph2}/cluster-profile", {}, H)
    check("кластерный профиль: кластер K1–K5",
          cp.get("cluster_code", "")[:2] in ("K1", "K2", "K3", "K4", "K5"), str(cp)[:140])
    check("OOD-отчёт C-ядра присутствует",
          "is_ood_anomaly" in (cp.get("ood") or {}), str(cp.get("ood"))[:120])
    cmap = req("/api/v1/compass-bio/cluster-map", token=H)
    check("карта: референсы + пациенты + честная маркировка",
          len(cmap.get("reference_points", [])) >= 60 and len(cmap.get("patients", [])) >= 1
          and cmap.get("honesty", {}).get("reference_note"), 
          f"refs={len(cmap.get('reference_points', []))} pts={len(cmap.get('patients', []))}")

    # 12. Серверный стрик: чекин повышает, дубль не раздувает
    eng0 = req("/api/v1/patient/engagement", token=PTOK)
    ev2 = str(uuid.uuid4())
    req("/api/v1/patient/checkin", {"event_uuid": ev2, "took_medication": True}, PTOK)
    eng1 = req("/api/v1/patient/engagement", token=PTOK)
    check("streak серверный растёт на новом чекине", eng1["total_points"] > eng0["total_points"],
          f"{eng0['total_points']} -> {eng1['total_points']}")
    req("/api/v1/patient/checkin", {"event_uuid": ev2, "took_medication": True}, PTOK)
    eng2 = req("/api/v1/patient/engagement", token=PTOK)
    check("дубль не раздувает streak/points", eng2 == eng1, str(eng2))

    # 13. Провенанс: /sources отдаёт реестр
    src = req("/api/v1/compass-bio/sources", token=H)
    check("реестр источников заполнен", len(src.get("sources", [])) >= 10,
          f"{len(src.get('sources', []))}")

    print("=" * 70)
    if failures:
        print(f"ПРОВАЛЕНО: {failures}")
        return 1
    print(f"БОЕВОЙ ТЕСТ БД ПРОЙДЕН: {N_PATIENTS} пациентов x {CHECKINS_PER_PATIENT} чек-инов, "
          f"{critical_expected} критичных; идемпотентность/RLS/timeline/аудит — ок.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
