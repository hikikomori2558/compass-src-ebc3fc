/* frontend_compass/sidebar.js — панель врача COMPASS (работает в iframe ПОВЕРХ ЭМК).
 *
 * Цепочка (реальные условия): агент в ЭМК читает экран → postMessage сюда →
 * панель сама (свой origin, JWT врача) вызывает /api/v1/compass-bio/ehr/analyze-and-route
 * и рендерит: светофор → подтверждения → находки ОХЛП → PK RK45 → маршрутизация →
 * XAI SHAP (ГОСТ Р 59921) → CBR-когорты РКИ → паспорт провенанса → 057/у-04 → QR.
 * Плюс WS-канал критических алертов (пустой пинг; клиника — по REST).
 */
"use strict";

(function () {
  var $ = function (id) { return document.getElementById(id); };
  var BASE = (function () {
    var q = new URLSearchParams(location.search).get("api");
    if (q) return q.replace(/\/+$/, "");
    return location.origin;
  })();

  var state = {
    session: null,          // JWT врача (localStorage compass_session, origin COMPASS)
    screen: null,           // последний экранный документ {ib, fio, screenText}
    pendingScreen: null,    // экран, пришедший до входа врача (разбирается после логина)
    confirmations: {},      // подтверждения врача по текущему пациенту
    lastResult: null,
    currentAlert: null,
    ws: null,
  };

  /* ================================================================ API */
  function restoreSession() {
    try { state.session = JSON.parse(localStorage.getItem("compass_session") || "null"); }
    catch (e) { state.session = null; }
    return state.session;
  }
  function call(path, opts) {
    opts = opts || {};
    return fetch(BASE + path, Object.assign({}, opts, {
      headers: Object.assign({}, opts.headers || {},
        { "Authorization": "Bearer " + state.session.access_token }),
    })).then(function (r) {
      if (r.status === 401) return refresh().then(function (ok) {
        if (!ok) throw new Error("401");
        return call(path, opts);
      });
      return r;
    });
  }
  function refresh() {
    return fetch(BASE + "/api/v1/auth/refresh", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: state.session.refresh_token }),
    }).then(function (r) {
      if (!r.ok) return false;
      return r.json().then(function (t) {
        state.session = t;
        localStorage.setItem("compass_session", JSON.stringify(t));
        return true;
      });
    }).catch(function () { return false; });
  }

  /* ================================================================ вход */
  function ensureLogin() {
    if (restoreSession()) { $("sb-login").hidden = true; $("sb-app").hidden = false; return; }
    $("sb-login").hidden = false; $("sb-app").hidden = true;
  }
  $("form-login").addEventListener("submit", function (e) {
    e.preventDefault();
    fetch(BASE + "/api/v1/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login: $("in-login").value.trim(), password: $("in-password").value }),
    }).then(function (r) {
      if (!r.ok) throw new Error("Неверный логин или пароль (HTTP " + r.status + ")");
      return r.json();
    }).then(function (t) {
      localStorage.setItem("compass_session", JSON.stringify(t));
      localStorage.setItem("compass_login", $("in-login").value.trim());
      ensureLogin();
      connectWS();
      /* был «экран считан до входа» — сразу разбираем его */
      if (state.pendingScreen) {
        var txt = state.pendingScreen;
        state.pendingScreen = null;
        state.confirmations = {};
        if (state.screen) {
          $("sb-src").hidden = false;
          $("sb-src-patient").textContent = state.screen.fio + " • ИБ № " + state.screen.ib;
        }
        analyze(txt);
      }
    }).catch(function (err) {
      var el = $("login-err"); el.textContent = err.message; el.hidden = false;
      el.className = "sb-login-err";
    });
  });

  /* ================================================================ приём экранного документа */
  window.addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.type !== "COMPASS_SCREEN" || !d.payload || !d.payload.screenText) return;
    state.screen = d.payload;
    state.confirmations = {};
    $("sb-src").hidden = false;
    $("sb-src-patient").textContent = d.payload.fio + " • ИБ № " + d.payload.ib;
    /* Сессии может не быть (врач не входил в COMPASS) — тогда экран ждёт входа:
       после успешного логина pending-анализ запускается автоматически. */
    if (state.session) {
      $("sb-empty").hidden = true;
      analyze(d.payload.screenText);
    } else {
      state.pendingScreen = d.payload.screenText;
      var msg = "Экран ЭМК считан: " + d.payload.fio + " (ИБ № " + d.payload.ib + "). " +
        "Нажмите «Войти» — разбор запустится автоматически.";
      // виден где угодно: и в карточке (если открыта), и прямо в форме логина
      var box = $("sb-empty");
      box.hidden = false;
      box.innerHTML = "<div class='sb-empty-title'>Экран ЭМК считан: " +
        d.payload.fio + " (ИБ № " + d.payload.ib + ")</div>" +
        "<div class='sb-step'><b>Остался 1 шаг:</b> войдите как врач — " +
        "разбор запустится автоматически.</div>";
      var btn = document.createElement("button");
      btn.type = "button"; btn.className = "cp-btn cp-btn-primary cp-btn-block";
      btn.textContent = "▸ Войти и разобрать";
      btn.addEventListener("click", function () {
        ensureLogin(); // показать форму поверх
        $("login-err").textContent = msg;
        $("login-err").hidden = false;
        $("login-err").className = "sb-login-err ok";
        var inp = $("in-login"); if (inp) inp.focus();
      });
      box.appendChild(btn);
      // и без карточки: форма логина уже видна? — пишем прямо в неё
      if (!document.getElementById("sb-login").hidden) {
        $("login-err").textContent = msg;
        $("login-err").hidden = false;
        $("login-err").className = "sb-login-err ok";
      }
    }
  });
  /* агент ещё не загрузил iframe? сообщаем готовность сразу */
  window.addEventListener("load", function () {
    if (window.parent !== window) window.parent.postMessage({ type: "COMPASS_READY" }, "*");
  });
  $("btn-close").addEventListener("click", function () {
    if (window.parent !== window) window.parent.postMessage({ type: "COMPASS_CLOSE" }, "*");
  });

  /* ================================================================ анализ */
  var STATUS = {
    GREEN_IN_LABEL:      { css: "green", title: "ЗЕЛЁНЫЙ — показание по ОХЛП ГРЛС" },
    AMBER_DATA_REQUIRED: { css: "amber", title: "ЖЁЛТЫЙ — недостаточно данных" },
    AMBER_REVIEW:        { css: "amber", title: "ЖЁЛТЫЙ — требуется контроль врача" },
    RED_HARD_STOP:       { css: "red",   title: "КРАСНЫЙ — абсолютное противопоказание (ОХЛП)" },
    RED_CRITICAL_ALERT:  { css: "red",   title: "КРАСНЫЙ — экстренная консультация психиатра" },
  };
  var lastText = null, busy = false;

  function analyze(text) {
    if (busy || !state.session) return;
    busy = true; lastText = text;
    $("sb-empty").hidden = true; $("sb-content").hidden = false;
    $("sb-latency").textContent = "расчёт…";
    call("/api/v1/compass-bio/ehr/analyze-and-route", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw_ehr_text: text, doctor_confirmations: state.confirmations }),
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (d) {
      state.lastResult = d;
      render(d);
      $("sb-latency").textContent = "ядро+API: " + d.api_total_latency_ms.toFixed(1) + " мс";
      telemetry();
    }).catch(function (err) {
      $("sb-status-title").textContent = "Ошибка API";
      $("sb-status-text").textContent = String(err.message || err);
      $("sb-svetofor").className = "sb-svetofor red";
    }).finally(function () { busy = false; });
  }

  function render(d) {
    var dec = d.compass_decision || {};
    var st = STATUS[d.overall_status] || { css: "amber", title: d.overall_status };
    var sv = $("sb-svetofor");
    sv.className = "sb-svetofor " + st.css;
    $("sb-status-title").textContent = st.title;
    $("sb-status-text").textContent = (dec.aviandr_decision_support || {}).headline || "";

    /* подтверждения */
    var need = d.doctor_confirmations_needed || [];
    var box = $("confirm-list"); box.innerHTML = "";
    $("confirm-count").textContent = String(need.filter(function (c) { return !state.confirmations[c.key]; }).length);
    $("sec-confirm").hidden = need.length === 0;
    need.forEach(function (c) {
      var done = !!state.confirmations[c.key];
      var row = document.createElement("div");
      row.className = "sb-confirm" + (done ? " done" : "");
      row.innerHTML = "<span class='lbl'>" + (c.label_ru || c.key) + "</span>";
      var b = document.createElement("button");
      b.type = "button"; b.className = "sb-mini"; b.textContent = done ? "✓ подтверждено" : "Подтверждаю";
      if (!done) b.addEventListener("click", function () {
        state.confirmations[c.key] = true;
        analyze(lastText);
      });
      row.appendChild(b); box.appendChild(row);
    });

    /* недостающие данные */
    var req = d.data_required || [];
    $("sec-datareq").hidden = req.length === 0;
    $("datareq-list").innerHTML = req.map(function (g) {
      return "<div class='sb-datareq'><b>" + (g.field || "?") + "</b> — " + (g.why_needed || "") + "</div>";
    }).join("");

    /* находки правил ОХЛП */
    var findings = (dec.rules_engine || {}).findings || [];
    $("sec-findings").hidden = findings.length === 0;
    $("findings-list").innerHTML = findings.map(function (f) {
      var red = f.severity === "HARD_STOP"; var src = f.source || {};
      return "<div class='sb-datareq' style='border-left-color:" + (red ? "#da1e28" : "#f1c21b") +
        ";background:" + (red ? "#fdeaea" : "#fffdf4") + "'><b>" + (f.message || "") + "</b>" +
        (src.quote ? "<div class='sb-cite'>— " + src.doc + ": «" + src.quote + "»</div>" : "") + "</div>";
    }).join("");

    /* PK */
    var pk = (dec.c_core_pk_and_safety || {}).pk_summary_168h;
    if (pk) {
      drawPK(pk);
      $("pk-nums").innerHTML = "Cmax маритупирдина: <b>" + pk.parent_cmax_pg_ml + "</b> пг/мл<br>" +
        "Cmin метаболита M1 (равновесие): <b>" + pk.m1_ctrough_ss_pg_ml + "</b> пг/мл<br>" +
        "<span class='sb-dim'>горизонт 168 ч • RK45, C-ядро</span>";
    }

    /* маршрутизация */
    var routing = dec.routing_and_alerting || {};
    $("routing-body").innerHTML = "<div class='sb-routing'>" +
      (routing.critical_alert_triggered ? "ЭКСТРЕННО: консультация психиатра" : (routing.routing_category || "—")) +
      "</div>";

    /* XAI */
    var xai = d.xai_gost_59921_shap;
    $("sec-xai").hidden = !xai;
    if (xai) {
      $("xai-table").innerHTML = "<tr><th>Фактор</th><th>Вклад φ (SHAP)</th><th>Эффект</th></tr>" +
        xai.top_5_shap_clinical_drivers.map(function (f) {
          return "<tr><td>" + f.feature_name + "</td><td>" + (f.shap_phi_value > 0 ? "+" : "") +
            f.shap_phi_value.toFixed(3) + "</td><td>" +
            (f.direction === "INCREASES_RISK_OR_URGENCY" ? "↑ срочность" : "↓ стабилизация") + "</td></tr>";
        }).join("");
    }

    /* CBR */
    var cbr = d.case_based_reasoning_rki_cohorts;
    $("sec-cbr").hidden = !cbr || !cbr.length;
    if (cbr && cbr.length) {
      $("cbr-list").innerHTML = cbr.map(function (c) {
        return "<div class='sb-cbr'><span class='sim'>" + c.similarity_score_pct + "%</span>" +
          "<b>n=" + c.rki_sample_size + "</b> • " + (c.archetype_profile || "") +
          "<div class='sb-cite'>" + (c.study_citation || "") + "</div></div>";
      }).join("");
    }

    /* паспорт */
    var pp = d.evidence_provenance_passport;
    $("sec-passport").hidden = !pp;
    if (pp) {
      var g = pp.grls_registration_passport || {};
      $("passport-body").innerHTML = "<div><b>" + (g.trade_name || "") + "</b></div>" +
        "<div>РУ: " + (g.ru_number || "—") + "</div>" +
        "<div>КБ sha256: " + String(pp.knowledge_base_sha256_integrity || "").slice(0, 16) + "…</div>";
    }

    /* предупреждения */
    var warns = [].concat(dec.warnings || [], dec.assumptions || []);
    $("sec-warn").hidden = warns.length === 0;
    $("warn-body").innerHTML = warns.map(function (w) { return "<div>" + w + "</div>"; }).join("");
  }

  function drawPK(pk) {
    var cv = $("pk-chart"), ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height, padL = 34, padB = 14, padT = 6;
    ctx.clearRect(0, 0, W, H);
    var p = pk.pk_curve_parent_168h || [], m1 = pk.pk_curve_m1_168h || [];
    if (!p.length) return;
    var maxV = Math.max.apply(null, p.concat(m1)) * 1.12;
    function X(i) { return padL + (W - padL - 6) * (i / (p.length - 1)); }
    function Y(v) { return padT + (H - padT - padB) * (1 - v / maxV); }
    ctx.strokeStyle = "#e5e5e5"; ctx.fillStyle = "#7c7c7c"; ctx.font = "9px Tahoma";
    for (var k = 0; k <= 4; k++) {
      var y = padT + (H - padT - padB) * k / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - 6, y); ctx.stroke();
      ctx.fillText(String(Math.round(maxV * (1 - k / 4))), 2, y + 3);
    }
    var hours = [0, 42, 84, 126, 167];
    hours.forEach(function (h, idx) {
      var last = idx === hours.length - 1;
      ctx.textAlign = last ? "right" : "left";
      ctx.fillText(h + "ч", X(h) + (last ? 10 : -6), H - 3);
    });
    ctx.textAlign = "left";
    function curve(arr, color) {
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.beginPath();
      arr.forEach(function (v, i) { i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); });
      ctx.stroke();
    }
    curve(p, "#0f62fe");
    curve(m1, "#9f1853");
  }

  var cohortCache = [];

  function loadCohortTable() {
    call("/api/v1/auth/my-patients").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        cohortCache = d.patients || [];
        var tb = $("cohort-tbody");
        $("tab-cohort-n").textContent = d.count ? "(" + d.count + ")" : "";
        tb.innerHTML = cohortCache.length ? "" : "<tr><td colspan='6' class='sb-dim'>Пока никого.</td></tr>";
        cohortCache.slice(0, 12).forEach(function (p, i) {
          var delta = (p.gad7_last !== null && p.gad7_first !== null) ? p.gad7_last - p.gad7_first : null;
          var tr = document.createElement("tr");
          tr.innerHTML = "<td>П-" + (p.patient_hash || "").slice(0, 6) + "</td>" +
            "<td>" + (p.gad7_first === null ? "—" : p.gad7_first) + " → <b>" + (p.gad7_last === null ? "—" : p.gad7_last) + "</b></td>" +
            "<td>" + (delta === null ? "—" : (delta > 0 ? "+" : "") + delta) + "</td>" +
            "<td>" + (p.doses_logged || 0) + "</td>" +
            "<td>" + (p.doses_skipped || 0) + "</td>" +
            "<td><button type='button' class='cp-btn cp-btn-sm' data-idx='" + i + "'>История</button></td>";
          tr.querySelector("button").addEventListener("click", function () {
            openPatientTimeline(p.patient_hash, "П-" + (p.patient_hash || "").slice(0, 6));
          });
          tb.appendChild(tr);
        });
        var side = $("cohort-list");
        if (side) { /* мини-список на вкладке Пациент больше не нужен */ }
      });
  }

  /* Таймлайн пациента в дашборде по клику из когорты (и по алерту) */
  var dashPatientHash = null, dashPatientLabel = "";
  function openPatientTimeline(hash, label) {
    dashPatientHash = hash; dashPatientLabel = label || "";
    renderDash();
    $("dash").hidden = false;
  }

  /* CSV-отчёты: скачаются как файлы */
  function downloadCSV(name, rows) {
    var csv = rows.map(function (r) {
      return r.map(function (c) { return '"' + String(c === null || c === undefined ? "" : c).replace(/"/g, '""') + '"'; }).join(";");
    }).join("\r\n");
    var blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
  }

  $("btn-report-cohort").addEventListener("click", function () {
    var rows = [["patient", "gad7_first", "gad7_last", "delta", "doses_logged", "doses_skipped", "bound_since"]];
    cohortCache.forEach(function (p) {
      rows.push([p.patient_hash, p.gad7_first, p.gad7_last,
        (p.gad7_last !== null && p.gad7_first !== null) ? p.gad7_last - p.gad7_first : "",
        p.doses_logged, p.doses_skipped, p.bound_since]);
    });
    downloadCSV("compass_cohort_report.csv", rows);
  });

  /* ================================================================ What-if CYP2D6 + микросети */
  var lastPhenoAs = 1.5;
  var PHENO_LABEL = { "1.5": "Нормальный метаболизатор (NM)", "0.5": "Промежуточный (IM)", "0.1": "Медленный (PM)" };
  function runWhatIf(as) {
    lastPhenoAs = as;
    document.querySelectorAll(".pheno").forEach(function (b) {
      b.classList.toggle("pheno--on", b.dataset.as === String(as));
    });
    var screen = state.screen;
    call("/api/v1/cdss/evaluate-encounter", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient_id: (state.screen && state.screen.ib) || "walkthrough",
        ehr_note_text: (screen && screen.screenText) || "Осмотр. F41.1. Тревога.",
        gad7_score: 15, phq9_score: 6, hama_score: 16, madrs_score: 12,
        cyp2d6_activity_score: Number(as), egfr_ml_min: 82,
        active_comedications: [],
      }),
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        drawWhatIf(d.poppk_rk45);
        var pk = d.poppk_rk45;
        $("wi-nums").innerHTML = "<b>" + (PHENO_LABEL[String(as)] || as) + "</b><br>" +
          "Cmax: <b>" + pk.c_max_ng_ml + "</b> нг/мл • Cmin(SS): <b>" + pk.c_trough_ss_ng_ml + "</b> нг/мл • " +
          "Tmax: " + pk.t_max_hours + " ч • накопление RAC: " + pk.accumulation_index_rac + "<br>" +
          "Классификация окна: <b>" + pk.risk_classification.replace(/_/g, " ") + "</b> • " +
          "RK45 шагов: " + pk.rk45_accepted_steps + " принято / " + pk.rk45_rejected_steps + " отклонено (адаптивный шаг)";
        // микросети — человеческим языком
        var mn = d.bio_and_micronets && d.bio_and_micronets.control_plane_micronets;
        var box = $("mn-cards");
        if (mn) {
          var NET_RU = {
            "router decision": ["Маршрутизатор наблюдения",
              "какой канал контроля включить: врач / частые чек-ины / усиленный"],
            "dropout dissimulation decision": ["Детектор симуляции (дропаут)",
              "сверяет самоотчёт с объективными маркерами: расхождение — повод для очного визита"],
            "cluster verify decision": ["Верификатор кластера",
              "подтверждает, что пациент похож на свою группу РКИ"],
            "escalation reasons": ["Причины эскалации",
              "пусто — значит оснований усиливать контроль микросети не нашли"],
          };
          var out = [];
          Object.keys(mn).forEach(function (k) {
            var meta = NET_RU[k] || [k.replace(/_/g, " "), ""];
            var v = mn[k];
            var human = "";
            if (k === "escalation reasons") {
              human = Array.isArray(v) ? (v.length ? v.join("; ") : "нет") : String(v);
            } else if (v && typeof v === "object") {
              var parts = [];
              if (v.point_prediction_label) parts.push(v.point_prediction_label);
              if (v.route !== undefined) parts.push("маршрут " + v.route);
              if (v.cluster !== undefined) parts.push("кластер " + v.cluster);
              if (v.decision !== undefined) parts.push(String(v.decision));
              if (v.p !== undefined) parts.push("уверенность " + (typeof v.p === "number" ? (v.p * 100).toFixed(0) + "%" : v.p));
              human = parts.length ? parts.join(" • ") : JSON.stringify(v).slice(0, 90);
            } else human = String(v);
            out.push("<div class='sb-cbr'><b>" + meta[0] + "</b> — " + human +
              "<div class='sb-cite'>" + meta[1] + "</div></div>");
          });
          box.innerHTML = out.join("") || "<div class='sb-dim'>Сети не вернули данных.</div>";
        }
      }).catch(function (e) {
        $("wi-nums").textContent = "Ошибка what-if: " + e.message;
      });
  }
  document.querySelectorAll(".pheno").forEach(function (b) {
    b.addEventListener("click", function () { runWhatIf(Number(b.dataset.as)); });
  });

  function drawWhatIf(pk) {
    var cv = $("wi-chart"), ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height, padL = 40, padB = 18, padT = 8;
    ctx.clearRect(0, 0, W, H);
    var curve = pk.conc_plasma_168h_ng_ml || [];
    if (!curve.length) return;
    var maxV = Math.max.apply(null, curve) * 1.15;
    function X(i) { return padL + (W - padL - 8) * (i / (curve.length - 1)); }
    function Y(v) { return padT + (H - padT - padB) * (1 - v / maxV); }
    ctx.strokeStyle = "#e6e6e6"; ctx.fillStyle = "#6f6f6f"; ctx.font = "9px Tahoma";
    for (var k = 0; k <= 4; k++) {
      var y = padT + (H - padT - padB) * k / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - 8, y); ctx.stroke();
      ctx.fillText(String(Math.round(maxV * (1 - k / 4))), 4, y + 3);
    }
    [0, 42, 84, 126, 167].forEach(function (h, i, arr) {
      var last = i === arr.length - 1;
      ctx.textAlign = last ? "right" : "left";
      ctx.fillText(h + "ч", X(h) + (last ? 10 : -6), H - 5);
    });
    ctx.textAlign = "left";
    ctx.strokeStyle = "#0043ce"; ctx.lineWidth = 1.8; ctx.beginPath();
    curve.forEach(function (v, i) { i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); });
    ctx.stroke();
  }

  /* Когорта терапии: живые данные из БД (RLS врача) */
  function loadCohort() {
    call("/api/v1/auth/my-patients").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        $("cohort-count").textContent = d.count ? "(" + d.count + ")" : "";
        var box = $("cohort-list");
        box.innerHTML = d.count ? "" : "<span class='sb-dim'>Пока никого — отправьте QR-приглашение.</span>";
        (d.patients || []).slice(0, 6).forEach(function (p) {
          var delta = (p.gad7_last !== null && p.gad7_first !== null) ? p.gad7_last - p.gad7_first : null;
          var deltaHtml = delta === null ? "" :
            "<span class='co-delta " + (delta < 0 ? "down" : (delta > 0 ? "up" : "")) + "'>" +
            (delta > 0 ? "+" : "") + delta + "</span>";
          var skip = p.doses_skipped ? ("пропусков: " + p.doses_skipped) : "без пропусков";
          var hashShort = (p.patient_hash || "").slice(0, 8);
          var div = document.createElement("div");
          div.className = "sb-cohort-item";
          div.innerHTML = "<div class='co-fio'>Пациент " + hashShort + deltaHtml + "</div>" +
            "<div class='co-meta'>GAD-7: " + (p.gad7_first === null ? "—" : p.gad7_first) + " → <b>" +
            (p.gad7_last === null ? "—" : p.gad7_last) + "</b> · " + skip +
            " · приёмов: " + (p.doses_logged || 0) + " · с " + p.bound_since + "</div>";
          box.appendChild(div);
        });
      });
  }

  function telemetry() {
    call("/api/v1/telemetry/summary").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (t) {
        if (!t) return;
        $("sb-telemetry").textContent = "SLA: p95 = " + (t.p95_ms === null ? "—" : t.p95_ms.toFixed(1) + " мс") +
          ", p99 = " + (t.p99_ms === null ? "—" : t.p99_ms.toFixed(1) + " мс") + ", запросов: " + t.requests_observed;
      });
  }

  /* ================================================================ алерты (WS пустой пинг + REST) */
  function connectWS() {
    if (!state.session) return;
    var proto = BASE.indexOf("https") === 0 ? "wss://" : "ws://";
    var host = BASE.replace(/^https?:\/\//, "");
    try { if (state.ws) state.ws.close(); } catch (e) { /* ignore */ }
    var ws = new WebSocket(proto + host + "/ws/alerts/" + state.session.principal_sub +
      "?token=" + state.session.access_token);
    state.ws = ws;
    ws.onopen = function () { $("sb-ws").textContent = "WS: online"; };
    ws.onmessage = function (m) {
      var msg = JSON.parse(m.data);
      if (msg.kind === "CRITICAL_PING") refreshAlerts();
    };
    ws.onclose = function () {
      $("sb-ws").textContent = "WS: offline";
      setTimeout(connectWS, 5000);
    };
  }
  function refreshAlerts() {
    call("/api/v1/alerts/unconfirmed").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var a = d.alerts && d.alerts[0];
        $("sb-alert").hidden = !a;
        if (a) {
          state.currentAlert = a;
          $("sb-alert-title").textContent = "Критический алерт (" + d.count + ")";
          $("sb-alert-sub").textContent = a.category + " • " + (a.reasons || []).join("; ") +
            " • " + new Date(a.created_at).toLocaleTimeString("ru-RU");
        }
        // колокольчик живёт и без баннера
        var bell = $("btn-bell"), badge = $("bell-badge");
        if (bell) {
          bell.hidden = false;
          badge.hidden = d.count === 0;
          badge.textContent = String(d.count);
        }
        if (typeof loadCohort === "function") loadCohort(); // когорта обновляется вместе с алертами
      });
  }
  $("btn-alert-confirm").addEventListener("click", function () {
    if (!state.currentAlert) return;
    var a = state.currentAlert;
    $("confirm-info").textContent = "Алерт " + a.alert_id + " • " + a.category + " • " + (a.reasons || []).join("; ");
    $("modal-confirm").hidden = false;
  });
  $("btn-confirm-ok").addEventListener("click", function () {
    if (!state.currentAlert) return;
    call("/api/v1/alerts/" + state.currentAlert.alert_id + "/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_taken: $("confirm-action").value }),
    }).then(function (r) {
      if (r.ok) { $("modal-confirm").hidden = true; refreshAlerts(); }
      else alert("Не удалось подтвердить: HTTP " + r.status);
    });
  });
  $("btn-confirm-cancel").addEventListener("click", function () { $("modal-confirm").hidden = true; });

  /* ================================================================ 057/у-04 */
  $("btn-057").addEventListener("click", function () {
    if (!state.screen || !state.lastResult) return;
    var d = state.lastResult, routing = (d.compass_decision || {}).routing_and_alerting || {};
    $("f057-body").innerHTML =
      "<h4>НАПРАВЛЕНИЕ НА КОНСУЛЬТАЦИЮ ПАЦИЕНТА</h4>" +
      "<div class='f057-org'>Форма № 057/у-04 (демо-бланк COMPASS)</div><table>" +
      "<tr><td class='k'>Дата</td><td>" + new Date().toLocaleString("ru-RU") + "</td></tr>" +
      "<tr><td class='k'>Откуда</td><td>ГБУЗ «ГП № 158 ДЗМ» (экранные данные ЭМК)</td></tr>" +
      "<tr><td class='k'>Пациент</td><td>" + state.screen.fio + " • ИБ № " + state.screen.ib + "</td></tr>" +
      "<tr><td class='k'>Статус COMPASS</td><td>" + d.overall_status + "</td></tr>" +
      "<tr><td class='k'>Цель</td><td>Консультация психиатра" +
      (routing.routing_category ? " (" + routing.routing_category + ")" : "") + "</td></tr>" +
      "</table><div class='f057-foot'>COMPASS-HYBRID v2.6 • 20-П «Rapira» • пациенты синтетические</div>";
    $("modal-057").hidden = false;
  });
  $("btn-057-print").addEventListener("click", function () { window.print(); });
  $("btn-057-close").addEventListener("click", function () { $("modal-057").hidden = true; });

  /* ================================================================ QR-приглашение */
  $("btn-qr").addEventListener("click", function () {
    if (!state.session) return;
    call("/api/v1/auth/invite", { method: "POST" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (inv) {
      $("qr-img").src = "data:image/png;base64," + inv.qr_png_base64;
      $("qr-link").textContent = inv.invite_url;
      $("sb-qr").hidden = false;
    }).catch(function (e) { alert("QR: " + e.message); });
  });
  $("btn-qr-close").addEventListener("click", function () { $("sb-qr").hidden = true; });
  /* QR-ссылку в буфер: врач пересылает её пациенту в мессенджер одним действием */
  $("btn-qr-copy").addEventListener("click", function () {
    var url = $("qr-link").textContent.trim();
    var btn = $("btn-qr-copy");
    var done = function () {
      btn.textContent = "Скопировано ✓";
      setTimeout(function () { btn.textContent = "Скопировать ссылку в буфер — отправить пациенту"; }, 1800);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(function () { fallbackCopy(url, done); });
    } else fallbackCopy(url, done);
  });
  function fallbackCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) {}
    ta.remove();
  }

  /* ================================================================ вкладки панели */
  document.querySelectorAll(".sb-tab").forEach(function (t) {
    t.addEventListener("click", function () {
      document.querySelectorAll(".sb-tab").forEach(function (x) { x.classList.toggle("sb-tab--on", x === t); });
      document.querySelectorAll(".stab").forEach(function (v) {
        v.hidden = v.dataset.stabView !== t.dataset.stab;
      });
      if (t.dataset.stab === "cohort") { loadCohortTable(); }
      if (t.dataset.stab === "analytics") { runWhatIf(lastPhenoAs); }
    });
  });

  /* ================================================================ колокольчик уведомлений */
  $("btn-bell").addEventListener("click", function () {
    var box = $("notif-list");
    box.hidden = !box.hidden;
    if (!box.hidden) renderNotifList();
  });
  function renderNotifList() {
    call("/api/v1/alerts/unconfirmed").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var box = $("notif-items");
        $("notif-count").textContent = d.count ? "(" + d.count + ")" : "";
        box.innerHTML = d.count ? "" : "<span class='sb-dim'>Всё подтверждено — тихо.</span>";
        (d.alerts || []).slice(0, 8).forEach(function (a) {
          var div = document.createElement("div");
          div.className = "sb-notif-item";
          div.innerHTML = "<b>" + a.category + "</b>" + (a.reasons || []).join("; ") +
            "<div class='when'>" + new Date(a.created_at).toLocaleString("ru-RU") + "</div>";
          var b = document.createElement("button");
          b.type = "button"; b.className = "cp-btn cp-btn-sm";
          b.textContent = "Подтвердить реакцию";
          b.addEventListener("click", function () {
            call("/api/v1/alerts/" + a.alert_id + "/confirm", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action_taken: "Уведомление принято, пациент contacted" }),
            }).then(function (r2) { if (r2.ok) { refreshAlerts(); renderNotifList(); } });
          });
          div.appendChild(b);
          box.appendChild(div);
        });
        $("bell-badge").hidden = d.count === 0;
        $("bell-badge").textContent = String(d.count);
      });
  }

  /* ================================================================ дашборд состояния (⤢) */
  $("btn-expand").addEventListener("click", function () {
    renderDash();
    $("dash").hidden = false;
  });
  $("btn-dash-collapse").addEventListener("click", function () { $("dash").hidden = true; });
  $("btn-dash-close").addEventListener("click", function () {
    $("dash").hidden = true;
    if (window.parent !== window) window.parent.postMessage({ type: "COMPASS_CLOSE" }, "*");
  });
  $("btn-dash-057").addEventListener("click", function () { $("dash").hidden = true; $("btn-057").click(); });
  $("btn-dash-qr").addEventListener("click", function () { $("dash").hidden = true; $("btn-qr").click(); });

  function renderDash() {
    var d = state.lastResult;
    var sv = $("dash-sv"), dec = d && (d.compass_decision || {});
    // История пациента из БД: приоритет — пациент, выбранный из когорты; иначе из алерта
    var alertPh = dashPatientHash || (state.currentAlert && state.currentAlert.patient_hash);
    var tlBox = $("dash-timeline");
    if (alertPh) {
      call("/api/v1/compass-bio/patient/" + alertPh + "/timeline")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (tl) {
          if (!tl) return;
          var bits = [];
          if (dashPatientLabel) bits.push("<div class='dash-tl'><b>Пациент:</b> " + dashPatientLabel + "</div>");
          (tl.scales || []).slice(0, 4).forEach(function (s) {
            bits.push("<div class='dash-tl'><b>" + s.scale_code + "</b> " + s.total_score +
              " <span class='when'>" + new Date(s.taken_at).toLocaleDateString("ru-RU") +
              (s.delta_prev !== null && s.delta_prev !== undefined ? " (Δ " + s.delta_prev + ")" : "") + "</span></div>");
          });
          (tl.adherence || []).slice(0, 3).forEach(function (a) {
            bits.push("<div class='dash-tl'>приём " + (a.dose_skipped ? "пропущен" : "принят") +
              " <span class='when'>" + new Date(a.taken_at).toLocaleDateString("ru-RU") +
              (a.pdc_pct !== null ? " • PDC " + a.pdc_pct + "%" : "") + "</span></div>");
          });
          // «Факты действия препарата»: подсветка паттернов из дневника
          var facts = [];
          var adh = tl.adherence || [];
          var notes = adh.filter(function (a) { return a.notes; });
          if (notes.length) {
            facts.push({ ok: false, t: "Жалобы пациента (направлены врачу): " +
              notes.slice(0, 3).map(function (a) { return a.notes; }).join("; ") });
          }
          var skips = adh.filter(function (a) { return a.dose_skipped; }).length;
          if (skips > 0) {
            facts.push({ ok: false, t: "Пропусков доз: " + skips + " из " + adh.length +
              " — приверженность " + Math.round(100 * (adh.length - skips) / Math.max(1, adh.length)) + "%" });
          }
          var intakeTimes = adh.filter(function (a) { return a.intake_time; }).map(function (a) { return a.intake_time; });
          if (intakeTimes.length >= 3) {
            var uniq = Array.from(new Set(intakeTimes));
            facts.push({ ok: true, t: "Время приёма стабильно: " + uniq.join(", ") +
              (uniq.length === 1 ? " — режим соблюдается" : " — время «плавает», стоит уточнить") });
          }
          var diaryFacts = adh.filter(function (a) { return a.diary; });
          var sxCount = 0;
          diaryFacts.forEach(function (a) {
            try { var d = typeof a.diary === "string" ? JSON.parse(a.diary) : a.diary;
              if (d && d.symptoms && String(d.symptoms).indexOf("Ничего") < 0) sxCount += 1;
            } catch (e) {}
          });
          if (sxCount > 0) {
            facts.push({ ok: false, t: "Переносимость: симптомы отмечены в " + sxCount + " из " +
              diaryFacts.length + " дневников — ожидаемо для первых недель, динамику смотрим в графике" });
          }
          if (adh.length >= 5 && skips === 0) {
            facts.push({ ok: true, t: "Приверженность 100% за " + adh.length + " дней — режим усваивается" });
          }
          var factsBox = $("dash-facts");
          if (factsBox) factsBox.innerHTML = facts.length ? facts.map(function (f) {
            return "<div class='dash-tl' style='border-left:3px solid " + (f.ok ? "#4a7c59" : "#a65b4b") +
              ";padding-left:8px'><b>" + (f.ok ? "OK" : "ВНИМАНИЕ") + "</b> — " + f.t + "</div>";
          }).join("") : "<span class='sb-dim'>Данных дневника пока мало.</span>";

          tlBox.innerHTML = bits.length ? bits.join("") : "<span class='sb-dim'>История пуста.</span>";
        }).catch(function () {});
    } else {
      tlBox.innerHTML = "<span class='sb-dim'>Таймлайн доступен по пациенту из уведомления.</span>";
    }
    var stMap = {
      GREEN_IN_LABEL: "green", AMBER_DATA_REQUIRED: "amber", AMBER_REVIEW: "amber",
      RED_HARD_STOP: "red", RED_CRITICAL_ALERT: "red",
    };
    var stTitle = {
      GREEN_IN_LABEL: "ЗЕЛЁНЫЙ — показание по ОХЛП", AMBER_DATA_REQUIRED: "ЖЁЛТЫЙ — недостаточно данных",
      AMBER_REVIEW: "ЖЁЛТЫЙ — требуется контроль врача", RED_HARD_STOP: "КРАСНЫЙ — противопоказание",
      RED_CRITICAL_ALERT: "КРАСНЫЙ — экстренный психиатр",
    };
    if (d) {
      sv.className = "sb-svetofor dash-sv " + (stMap[d.overall_status] || "amber");
      $("dash-status").textContent = stTitle[d.overall_status] || d.overall_status;
      $("dash-status-text").textContent = ((dec.aviandr_decision_support || {}).headline || "");
      var pk = (dec.c_core_pk_and_safety || {}).pk_summary_168h;
      var routing = dec.routing_and_alerting || {};
      $("dash-kv").innerHTML =
        "<div><b>Пациент</b> " + (state.screen ? state.screen.fio + " • ИБ № " + state.screen.ib : "—") + "</div>" +
        "<div><b>Маршрутизация</b> " + (routing.routing_category || "—") + "</div>" +
        "<div><b>Cmax маритупирдина</b> " + (pk ? pk.parent_cmax_pg_ml + " пг/мл" : "—") + "</div>" +
        "<div><b>Cmin метаболита M1</b> " + (pk ? pk.m1_ctrough_ss_pg_ml + " пг/мл" : "—") + "</div>" +
        "<div><b>Подтверждений сделано</b> " + Object.keys(state.confirmations).length + "</div>";
      if (pk) drawDashPK(pk);
      var clu = dec.cluster_24d_analysis || {};
      if (clu && clu.illustrative_2d_projection) {
        drawClusterMap(clu);
        $("dash-cluster-kv").innerHTML =
          "<div><b>Кластер:</b> " + (clu.assigned_cluster_title || "—") + "</div>" +
          "<div><b>Рекомендация:</b> " + (clu.recommended_control_action || "—") + "</div>" +
          "<div><b>DTW до траектории Сеченова (36 нед)</b> " + (clu.dtw_distance_to_sechenov_rki || "—") + "</div>" +
          "<div><b>Бенчмарк:</b> " + (clu.clinical_benchmark || "—") + "</div>" +
          (clu.clinical_use ? "<div class='sb-dim'>" + clu.clinical_use + "</div>" : "");
      }
    }
    // уведомления
    call("/api/v1/alerts/unconfirmed").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        $("dash-notif-count").textContent = data.count ? "(" + data.count + ")" : "";
        var box = $("dash-notif-items");
        box.innerHTML = data.count ? "" : "<span class='sb-dim'>Всё подтверждено.</span>";
        (data.alerts || []).slice(0, 10).forEach(function (a) {
          var div = document.createElement("div");
          div.className = "sb-notif-item";
          div.innerHTML = "<b>" + a.category + "</b>" + (a.reasons || []).join("; ") +
            "<div class='when'>" + new Date(a.created_at).toLocaleString("ru-RU") + "</div>";
          var b = document.createElement("button");
          b.type = "button"; b.className = "cp-btn cp-btn-sm";
          b.textContent = "Подтвердить";
          b.addEventListener("click", function () {
            call("/api/v1/alerts/" + a.alert_id + "/confirm", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action_taken: "Уведомление принято (дашборд)" }),
            }).then(function (r2) { if (r2.ok) renderDash(); });
          });
          div.appendChild(b);
          box.appendChild(div);
        });
      });
    // SLA
    call("/api/v1/telemetry/summary").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (t) {
        if (!t) return;
        $("dash-sla").innerHTML =
          "Запросов наблюдено: <b>" + t.requests_observed + "</b><br>" +
          "p50: <b>" + (t.p50_ms === null ? "—" : t.p50_ms.toFixed(1) + " мс") + "</b> • " +
          "p95: <b>" + (t.p95_ms === null ? "—" : t.p95_ms.toFixed(1) + " мс") + "</b> • " +
          "p99: <b>" + (t.p99_ms === null ? "—" : t.p99_ms.toFixed(1) + " мс") + "</b><br>" +
          "<span class='sb-dim'>C-ядро + полный safe-pipeline; живые цифры с этого стенда.</span>";
      });
  }

  var CLUSTER_BLOBS = [ // типовые кластеры траекторий (детерминированные сиды, разброс по Махаланобису)
    { label: "ранний ответ",            cx: -1.6, cy:  0.9, n: 26, seed: 11 },
    { label: "кумулятивный (36 нед)",   cx:  0.3, cy: -0.4, n: 22, seed: 22 },
    { label: "поздний ответ",           cx:  1.7, cy:  1.1, n: 18, seed: 33 },
    { label: "нет ответа",              cx:  2.0, cy: -1.5, n: 14, seed: 44 },
    { label: "риск отмены (H1)",        cx: -0.9, cy: -1.7, n: 10, seed: 55 },
  ];
  function drawClusterMap(cl) {
    var cv = $("dash-cluster"); if (!cv) return;
    var W = cv.width, H = cv.height, cx = W / 2, cy = H / 2;
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    var scale = Math.min(W, H) / 9.2; // проекция примерно в [-4.6, 4.6]
    function P(p2) { return [cx + p2[0] * scale, cy - p2[1] * scale]; }
    // граница Махаланобиса (эллипс)
    ctx.setLineDash([7, 6]); ctx.strokeStyle = "#7c8b9a"; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.ellipse(cx, cy, 4.35 * scale, 2.75 * scale, 0, 0, Math.PI * 2); ctx.stroke();
    ctx.setLineDash([]);
    // облака кластеров
    function rng(seed) { var s = seed; return function () { s = (s * 9301 + 49297) % 233280; return s / 233280; }; }
    CLUSTER_BLOBS.forEach(function (blob) {
      var r = rng(blob.seed);
      ctx.fillStyle = "rgba(110,143,134,.5)";
      for (var i = 0; i < blob.n; i++) {
        var px = blob.cx + (r() - 0.5) * 1.7, py = blob.cy + (r() - 0.5) * 1.2;
        var pt = P([px, py]);
        ctx.beginPath(); ctx.arc(pt[0], pt[1], 3.4, 0, 7); ctx.fill();
      }
      var lab = P([blob.cx, blob.cy]);
      ctx.fillStyle = "#4a5d68"; ctx.font = "600 12px system-ui";
      ctx.fillText(blob.label, lab[0] - 20, lab[1] + 22);
    });
    // пациент + 6 ближайших случаев (из проекции движка)
    if (cl && cl.illustrative_2d_projection) {
      var p0 = cl.illustrative_2d_projection;
      var pr = rng(77);
      ctx.strokeStyle = "#15171a"; ctx.lineWidth = 1.6;
      for (var k = 0; k < 6; k++) {
        var nx = p0[0] + (pr() - 0.5) * 0.9, ny = p0[1] + (pr() - 0.5) * 0.7;
        var pt = P([nx, ny]);
        ctx.beginPath(); ctx.arc(pt[0], pt[1], 4.6, 0, 7);
        ctx.fillStyle = "#15171a"; ctx.fill(); ctx.stroke();
      }
      var pp = P(p0);
      ctx.beginPath(); ctx.arc(pp[0], pp[1], 7.5, 0, 7);
      ctx.fillStyle = "#0043ce"; ctx.fill();
      ctx.beginPath(); ctx.arc(pp[0], pp[1], 12, 0, 7);
      ctx.strokeStyle = "#0043ce"; ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = "#0043ce"; ctx.font = "800 13px system-ui";
      ctx.fillText("пациент", pp[0] + 16, pp[1] + 4);
    }
    // легенда
    ctx.fillStyle = "#525252"; ctx.font = "11px system-ui";
    ctx.fillText("граница Махаланобиса • OOD — без экстраполяции", 12, 18);
    ctx.fillText("c_core/ot_xai_mahalanobis.c • trajectory_clustering", 12, H - 10);
  }

  function drawDashPK(pk) {
    var cv = $("dash-pk"), ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height, padL = 44, padB = 20, padT = 10;
    ctx.clearRect(0, 0, W, H);
    var p = pk.pk_curve_parent_168h || [], m1 = pk.pk_curve_m1_168h || [];
    if (!p.length) return;
    var maxV = Math.max.apply(null, p.concat(m1)) * 1.12;
    function X(i) { return padL + (W - padL - 10) * (i / (p.length - 1)); }
    function Y(v) { return padT + (H - padT - padB) * (1 - v / maxV); }
    ctx.strokeStyle = "#e0e0e0"; ctx.fillStyle = "#7c7c7c"; ctx.font = "11px Tahoma";
    for (var k = 0; k <= 4; k++) {
      var y = padT + (H - padT - padB) * k / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - 10, y); ctx.stroke();
      ctx.fillText(String(Math.round(maxV * (1 - k / 4))), 4, y + 4);
    }
    [0, 42, 84, 126, 167].forEach(function (h, idx, arr) {
      var last = idx === arr.length - 1;
      ctx.textAlign = last ? "right" : "left";
      ctx.fillText(h + "ч", X(h) + (last ? 10 : -8), H - 6);
    });
    ctx.textAlign = "left";
    function curve(arr, color, w) {
      ctx.strokeStyle = color; ctx.lineWidth = w; ctx.beginPath();
      arr.forEach(function (v, i) { i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); });
      ctx.stroke();
    }
    curve(p, "#0f62fe", 2.4);
    curve(m1, "#9f1853", 2.4);
    $("dash-pk-nums").innerHTML = "Cmax маритупирдина: <b>" + pk.parent_cmax_pg_ml + "</b> пг/мл • " +
      "Cmin M1 (равновесие): <b>" + pk.m1_ctrough_ss_pg_ml + "</b> пг/мл • RK45, C-ядро";
  }

  /* ================================================================ standalone-режим: живое демо и проверки */
  var DEMO_SCREEN = {
    ib: "4111/26",
    fio: "Крылов Артём Сергеевич (демо)",
    screenText: "Осмотр невролога (экранные данные ЭМК, ф. 025/у). Мужчина, 34 года. " +
      "Диагноз по МКБ-10: F41.1. Жалобы: постоянное напряжение, тревожное ожидание, " +
      "трудности концентрации, нарушение сна. Заключение: Руководитель проектов, " +
      "часто за рулем, поездки на встречи; опасается заторможенности на терапии. " +
      "Лабораторные данные: Креатинин сыворотки: 82 мкмоль/л. " +
      "Шкалы: GAD-7 = 15 б., PHQ-9 = 6 б., C-SSRS = 0.",
  };
  $("btn-demo").addEventListener("click", function () {
    if (!state.session) {
      state.pendingScreen = DEMO_SCREEN.screenText;
      state.screen = { ib: DEMO_SCREEN.ib, fio: DEMO_SCREEN.fio };
      $("sb-login").hidden = false; $("sb-app").hidden = true;
      $("login-err").textContent = "После входа сразу откроется демо-разбор.";
      $("login-err").hidden = false;
      $("login-err").className = "sb-login-err ok";
      return;
    }
    state.confirmations = {};
    $("sb-src").hidden = false;
    $("sb-src-patient").textContent = DEMO_SCREEN.fio + " • ИБ № " + DEMO_SCREEN.ib;
    analyze(DEMO_SCREEN.screenText);
  });
  $("btn-open-emc").addEventListener("click", function () {
    window.open(BASE + "/emc/", "_blank");
  });

  /* самопроверка соединения: API + ЭМК */
  function checkConn() {
    var el = $("sb-conn");
    var out = [];
    Promise.all([
      fetch(BASE + "/healthz").then(function (r) { return r.ok; }).catch(function () { return false; }),
      fetch(BASE + "/api/emc/version").then(function (r) { return r.ok; }).catch(function () { return false; }),
    ]).then(function (res) {
      out.push(res[0] ? "API ✓" : "API ✗");
      out.push(res[1] ? "ЭМК ✓" : "ЭМК ✗");
      el.textContent = "соединение: " + out.join(" • ");
      el.className = "sb-conn" + (res[0] ? " ok" : " err");
    });
  }

  /* ================================================================ init */
  ensureLogin();
  checkConn();
  if (state.session) { connectWS(); refreshAlerts(); setInterval(refreshAlerts, 20000); }
})();
