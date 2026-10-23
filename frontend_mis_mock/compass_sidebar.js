/* frontend_mis_mock/compass_sidebar.js — COMPASS-сайдбар поверх МИС-стенда.
 *
 * Живой API (никаких локальных фоллбэков — на демо это принципиально):
 *   POST /api/v1/auth/login                    — JWT врача (Bearer на всё)
 *   POST /api/v1/compass-bio/ehr/analyze-and-route — tri-state разбор ЭМК -> светофор
 *   GET  /api/v1/telemetry/summary             — SLA-панель (Метрики №1/№6)
 * Панель: светофор ОХЛП -> подтверждения врача -> PK RK45 168ч -> маршрутизация ->
 * XAI SHAP (ГОСТ Р 59921) -> CBR-когорты РКИ -> паспорт провенанса -> ф. 057/у-04.
 */
"use strict";

/* ------------------------------------------------------------------ API */
const CompassAPI = {
  base: (function () {
    // ?api=http://host:port — локальная разработка; на стенде стика живёт на том же origin
    const q = new URLSearchParams(location.search).get("api");
    if (q) return q.replace(/\/+$/, "");
    return localStorage.getItem("compass_api_base") || "";
  })(),

  session: null, // {access_token, refresh_token, principal_sub, principal_role}

  saveSession(s) {
    this.session = s;
    localStorage.setItem("mis_session", JSON.stringify(s));
  },
  restoreSession() {
    try { this.session = JSON.parse(localStorage.getItem("mis_session")); }
    catch (e) { this.session = null; }
    return this.session;
  },
  dropSession() { this.session = null; localStorage.removeItem("mis_session"); },

  async login(login, password) {
    const r = await fetch(this.base + "/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login, password }),
    });
    if (r.status === 401) throw new Error("Неверный логин или пароль");
    if (r.status === 503) throw new Error("БД недоступна — поднимите docker compose");
    if (!r.ok) throw new Error("Ошибка входа: HTTP " + r.status);
    const data = await r.json();
    this.saveSession(data);
    return data;
  },

  /** fetch с Bearer; при 401 — одна попытка refresh + повтор. */
  async call(path, opts) {
    opts = opts || {};
    const call = () => fetch(this.base + path, Object.assign({}, opts, {
      headers: Object.assign({}, opts.headers || {}, {
        "Authorization": "Bearer " + this.session.access_token,
      }),
    }));
    let r = await call();
    if (r.status === 401) {
      const ok = await this.refresh();
      if (ok) r = await call();
    }
    return r;
  },
  async refresh() {
    try {
      const r = await fetch(this.base + "/api/v1/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: this.session.refresh_token }),
      });
      if (!r.ok) return false;
      this.saveSession(await r.json());
      return true;
    } catch (e) { return false; }
  },

  async analyzeAndRoute(rawText, confirmations) {
    const r = await this.call("/api/v1/compass-bio/ehr/analyze-and-route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        raw_ehr_text: rawText,
        doctor_confirmations: confirmations || {},
      }),
    });
    if (!r.ok) throw new Error("analyze-and-route: HTTP " + r.status);
    return { data: await r.json(), latencyHeader: r.headers.get("x-latency-ms") };
  },

  async telemetrySummary() {
    const r = await this.call("/api/v1/telemetry/summary");
    return r.ok ? r.json() : null;
  },
};

/* ------------------------------------------------------------------ панель */
const CompassSidebar = {
  patient: null,        // текущая запись журнала (ФИО/ИБ/МКБ для 057/у-04)
  confirmations: {},    // накопленные подтверждения врача по текущему пациенту
  lastResult: null,
  busy: false,

  STATUS_MAP: {
    GREEN_IN_LABEL:      { css: "green", title: "ЗЕЛЁНЫЙ — показание по ОХЛП ГРЛС" },
    AMBER_DATA_REQUIRED: { css: "amber", title: "ЖЁЛТЫЙ — недостаточно данных" },
    AMBER_REVIEW:        { css: "amber", title: "ЖЁЛТЫЙ — требуется контроль врача" },
    RED_HARD_STOP:       { css: "red",   title: "КРАСНЫЙ — абсолютное противопоказание (ОХЛП)" },
    RED_CRITICAL_ALERT:  { css: "red",   title: "КРАСНЫЙ — экстренная консультация психиатра" },
  },

  reset(patient) {
    this.patient = patient || null;
    this.confirmations = {};
    this.lastResult = null;
    document.getElementById("csb-empty").hidden = false;
    document.getElementById("csb-body").hidden = true;
  },

  async analyze(rawText) {
    if (this.busy || !CompassAPI.session) return;
    this.busy = true;
    document.getElementById("csb-empty").hidden = true;
    document.getElementById("csb-body").hidden = false;
    document.getElementById("csb-latency").textContent = "расчёт…";
    try {
      const { data, latencyHeader } = await CompassAPI.analyzeAndRoute(rawText, this.confirmations);
      this.lastResult = data;
      this.render(data, rawText);
      const lat = data.api_total_latency_ms;
      document.getElementById("csb-latency").textContent =
        "ядро+API: " + lat.toFixed(1) + " мс";
      this.refreshTelemetryLine();
    } catch (e) {
      document.getElementById("csb-status-title").textContent = "Ошибка API";
      document.getElementById("csb-status-text").textContent = String(e.message || e);
      document.getElementById("csb-svetofor").className = "csb-svetofor red";
    } finally {
      this.busy = false;
    }
  },

  confirm(key) {
    if (!this.lastResult || !this._rawText) return;
    this.confirmations[key] = true;
    this.analyze(this._rawText); // повторный разбор уже с подтверждением
  },

  render(data, rawText) {
    this._rawText = rawText;
    const d = data.compass_decision || data; // совместимость: полный ответ bio-роутера
    const overall = data.overall_status || d.overall_status;
    const st = this.STATUS_MAP[overall] || { css: "amber", title: overall };

    /* светофор */
    const sv = document.getElementById("csb-svetofor");
    sv.className = "csb-svetofor " + st.css;
    document.getElementById("csb-status-title").textContent = st.title;
    document.getElementById("csb-status-text").textContent =
      (d.aviandr_decision_support && d.aviandr_decision_support.headline) || "";

    /* подтверждения врача */
    const need = data.doctor_confirmations_needed || d.doctor_confirmations_needed || [];
    const box = document.getElementById("csb-confirm-list");
    box.innerHTML = "";
    document.getElementById("csb-confirm-count").textContent = String(
      need.filter(c => !this.confirmations[c.key]).length);
    document.getElementById("csb-sec-confirm").hidden = need.length === 0;
    need.forEach(c => {
      const done = !!this.confirmations[c.key];
      const row = document.createElement("div");
      row.className = "csb-confirm-item" + (done ? " done" : "");
      row.innerHTML = '<span class="lbl">' + (c.label_ru || c.key) + "</span>";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "csb-mini";
      btn.textContent = done ? "✓ подтверждено" : "Подтверждаю";
      if (!done) btn.addEventListener("click", () => this.confirm(c.key));
      row.appendChild(btn);
      box.appendChild(row);
    });

    /* недостающие данные */
    const req = data.data_required || d.data_required || [];
    document.getElementById("csb-sec-datareq").hidden = req.length === 0;
    document.getElementById("csb-datareq-list").innerHTML = req.map(g =>
      '<div class="csb-datareq-item"><b>' + (g.field || "?") + "</b> — " + (g.why_needed || "") + "</div>"
    ).join("");

    /* предупреждения движка правил: врач обязан ВИДЕТЬ, почему жёлтый/красный
       (например, WARNING про вождение по ОХЛП или абсолютные противопоказания) */
    const findings = (d.rules_engine && d.rules_engine.findings) || [];
    document.getElementById("csb-sec-findings").hidden = findings.length === 0;
    document.getElementById("csb-findings-list").innerHTML = findings.map(f => {
      const red = f.severity === "HARD_STOP";
      const src = f.source || {};
      return '<div class="csb-datareq-item" style="border-left-color:' + (red ? "#da1e28" : "#f1c21b") +
        ';background:' + (red ? "#fdeaea" : "#fffdf4") + '"><b>' + (f.message || f.message_ru) + "</b>" +
        (src.quote ? '<div style="font-size:10.3px;color:#525252;margin-top:2px">— ' +
          src.doc + ": «" + src.quote + "»</div>" : "") + "</div>";
    }).join("");

    /* PK 168 ч */
    const pk = d.c_core_pk_and_safety && d.c_core_pk_and_safety.pk_summary_168h;
    if (pk) {
      this.drawPK(pk);
      document.getElementById("csb-pk-nums").innerHTML =
        "Cmax маритупирдина: <b>" + pk.parent_cmax_pg_ml + "</b> пг/мл<br>" +
        "Cmin метаболита M1 (равновесие): <b>" + pk.m1_ctrough_ss_pg_ml + "</b> пг/мл<br>" +
        "<span style='color:var(--gray-600)'>горизонт 168 ч &bull; RK45, C-ядро</span>";
    }

    /* маршрутизация */
    const routing = d.routing_and_alerting || {};
    const rc = routing.routing_category || "—";
    document.getElementById("csb-routing-body").innerHTML =
      '<div style="font-size:12.5px"><b>' +
      (routing.critical_alert_triggered ? "ЭКСТРЕННО: психиатр (алерт)" : rc) +
      "</b></div><div style='font-size:11px;color:#525252;margin-top:3px'>" +
      (routing.routing_reason || "") + "</div>";

    /* XAI SHAP */
    const xai = data.xai_gost_59921_shap;
    document.getElementById("csb-sec-xai").hidden = !xai;
    if (xai) {
      const t = document.getElementById("csb-xai-table");
      t.innerHTML = "<tr><th>Фактор</th><th>Вклад φ (SHAP)</th><th>Эффект</th></tr>" +
        xai.top_5_shap_clinical_drivers.map(f =>
          "<tr><td>" + f.feature_name + "</td><td>" + (f.shap_phi_value > 0 ? "+" : "") +
          f.shap_phi_value.toFixed(3) + "</td><td>" +
          (f.direction === "INCREASES_RISK_OR_URGENCY" ? "↑ срочность" : "↓ стабилизация") +
          "</td></tr>").join("");
    }

    /* CBR когорты РКИ */
    const cbr = data.case_based_reasoning_rki_cohorts;
    document.getElementById("csb-sec-cbr").hidden = !cbr || !cbr.length;
    if (cbr && cbr.length) {
      document.getElementById("csb-cbr-list").innerHTML = cbr.map(c =>
        '<div class="csb-cbr-item"><span class="sim">' + c.similarity_score_pct + "%</span>" +
        "<b>n=" + c.rki_sample_size + "</b> &bull; " + (c.archetype_profile || "") +
        '<div class="cite">' + (c.study_citation || "") + "</div></div>"
      ).join("");
    }

    /* паспорт провенанса */
    const pp = data.evidence_provenance_passport;
    document.getElementById("csb-sec-passport").hidden = !pp;
    if (pp) {
      const g = pp.grls_registration_passport || {};
      document.getElementById("csb-passport-body").innerHTML =
        "<div><b>" + (g.trade_name || "") + "</b></div>" +
        "<div>РУ: " + (g.ru_number || "—") + "</div>" +
        "<div>КБ sha256: " + String(pp.knowledge_base_sha256_integrity || "").slice(0, 16) + "…</div>";
    }

    /* предупреждения/допущения */
    const warns = [].concat(d.warnings || [], d.assumptions || []);
    document.getElementById("csb-sec-warn").hidden = warns.length === 0;
    document.getElementById("csb-warn-body").innerHTML =
      warns.map(w => "<div>" + w + "</div>").join("");

    /* 057/у-04 доступна всегда — форму заполняем из последнего решения */
    document.getElementById("btn-form-057").disabled = false;
  },

  drawPK(pk) {
    const cv = document.getElementById("csb-pk-chart");
    const ctx = cv.getContext("2d");
    const W = cv.width, H = cv.height, padL = 30, padB = 14, padT = 6;
    ctx.clearRect(0, 0, W, H);
    const p = pk.pk_curve_parent_168h || [], m1 = pk.pk_curve_m1_168h || [];
    if (!p.length) return;
    const maxV = Math.max.apply(null, p.concat(m1)) * 1.12;
    const X = i => padL + (W - padL - 4) * (i / (p.length - 1));
    const Y = v => padT + (H - padT - padB) * (1 - v / maxV);

    ctx.strokeStyle = "#e0e0e0"; ctx.fillStyle = "#6f6f6f"; ctx.font = "9px Tahoma";
    for (let k = 0; k <= 4; k++) {
      const y = padT + (H - padT - padB) * k / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - 4, y); ctx.stroke();
      ctx.fillText(Math.round(maxV * (1 - k / 4)), 2, y + 3);
    }
    // последняя метка прижата вправо, иначе «167ч» обрезается краем канвы
    const hours = [0, 42, 84, 126, 167];
    hours.forEach(h => {
      const last = h === hours[hours.length - 1];
      ctx.textAlign = last ? "right" : "left";
      ctx.fillText(h + "ч", X(h) + (last ? 8 : -6), H - 3);
    });
    ctx.textAlign = "left";

    const curve = (arr, color) => {
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.beginPath();
      arr.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
      ctx.stroke();
    };
    curve(p, "#15171a");   /* маритупирдин — фирменный тёмный Ghost */
    curve(m1, "#30cf43");  /* метаболит M1 — фирменный green-500 Ghost */
  },

  async refreshTelemetryLine() {
    const t = await CompassAPI.telemetrySummary();
    if (!t) return;
    document.getElementById("csb-telemetry").textContent =
      "SLA: p95 = " + (t.p95_ms === null ? "—" : t.p95_ms.toFixed(1) + " мс") +
      ", p99 = " + (t.p99_ms === null ? "—" : t.p99_ms.toFixed(1) + " мс") +
      ", запросов: " + t.requests_observed;
  },
};

window.CompassAPI = CompassAPI;
window.CompassSidebar = CompassSidebar;
