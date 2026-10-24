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
    // История пациента из БД: таймлайн последнего алерта (шкалы/приверженность/алерты)
    var alertPh = state.currentAlert && state.currentAlert.patient_hash;
    var tlBox = $("dash-timeline");
    if (alertPh) {
      call("/api/v1/compass-bio/patient/" + alertPh + "/timeline")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (tl) {
          if (!tl) return;
          var bits = [];
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
