# -*- coding: utf-8 -*-
"""
backend_emc/server.py — ДЕМО-СТЕНД «проприетарной ЭМК» (имитация системы вендора).

Это СОЗНАТЕЛЬНО отдельное приложение, не связанное с COMPASS:
  * отдельный процесс и порт (8100);
  * свой мини-API и « своя» БД пациентов (JSON на сервере, как у вендора);
  * ни одного импорта из backend/* (нашего CDSS) — вендор о COMPASS не знает;
  * COMPASS работает ПОВЕРХ этой системы, считывая данные с экрана (agent.js).

Запуск: python3 -m backend_emc.server   (порт 8100)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

EMC_ROOT = Path(__file__).resolve().parent.parent
PATIENTS_FILE = EMC_ROOT / "backend_emc" / "data" / "patients_today.json"
EMC_STATIC = EMC_ROOT / "frontend_emc"

app = FastAPI(title="АИС «Поликлиника» (демо-стенд ЭМК вендора)", version="1.0.7")

_notes: Dict[str, str] = {}  # «БД» заключений вендора (в памяти, как демо)


def _patients() -> List[Dict[str, Any]]:
    return json.loads(PATIENTS_FILE.read_text(encoding="utf-8"))


@app.get("/api/emc/version")
def version() -> Dict[str, str]:
    return {"product": "АИС «Поликлиника»", "vendor": "ООО «МедИнформСистемы» (демо)",
            "version": "1.0.7"}


@app.get("/api/emc/patients")
def patients() -> List[Dict[str, Any]]:
    """Журнал приёма на сегодня — данные только для отображения в ЭМК."""
    return [
        {k: p[k] for k in ("ib", "slot", "fio", "sex", "born", "oms", "icd10",
                           "doctor", "status", "tag", "case_ref", "source")}
        for p in _patients()
    ]


@app.get("/api/emc/patient/{ib:path}")  # ИБ вида «4111/26»: слэш — часть номера
def patient(ib: str) -> Dict[str, Any]:
    for p in _patients():
        if p["ib"] == ib:
            p = dict(p)
            p["note_saved"] = _notes.get(ib, "")
            return p
    raise HTTPException(404, "Пациент не найден")


class NoteIn(BaseModel):
    text: str


@app.post("/api/emc/patient/{ib:path}/note")
def save_note(ib: str, req: NoteIn) -> Dict[str, Any]:
    if not any(p["ib"] == ib for p in _patients()):
        raise HTTPException(404, "Пациент не найден")
    _notes[ib] = req.text
    return {"status": "saved", "bytes": len(req.text)}


app.mount("/", StaticFiles(directory=str(EMC_STATIC), html=True), name="emc")


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host=os.getenv("EMC_BIND", "127.0.0.1"), port=8100)
