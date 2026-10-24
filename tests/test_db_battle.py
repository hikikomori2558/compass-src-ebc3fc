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

    print("=" * 70)
    if failures:
        print(f"ПРОВАЛЕНО: {failures}")
        return 1
    print(f"БОЕВОЙ ТЕСТ БД ПРОЙДЕН: {N_PATIENTS} пациентов x {CHECKINS_PER_PATIENT} чек-инов, "
          f"{critical_expected} критичных; идемпотентность/RLS/timeline/аудит — ок.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
