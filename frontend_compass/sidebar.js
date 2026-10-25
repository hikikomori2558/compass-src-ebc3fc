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
      refreshAlerts();   // баннер критических алертов — сразу после входа, не только по WS-пингу
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
    sv.className = "sb-svetofor hero-verdict hero-" + st.css;
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
        $("tab-cohort-n").textContent = d.count ? "(" + d.count + ")" : "";
        renderCohortTable("");
        var search = $("cohort-search");
        if (search && !search.__bound) {
          search.__bound = true;
          search.addEventListener("input", function () { renderCohortTable(search.value.trim().toLowerCase()); });
        }
      });
  }

  /* Полная когорта (105 персон): ярлык, кластер с OOD-меткой, действия по строке.
   * Демо-вход и QR — только для синтетических (сервер это гарантирует). */
  function renderCohortTable(filter) {
    var tb = $("cohort-tbody");
    if (!tb) return;
    var rows = cohortCache.filter(function (p) {
      if (!filter) return true;
      var hay = ((p.label || "") + " П-" + (p.patient_hash || "").slice(0, 6) + " " +
                 (p.cluster || "")).toLowerCase();
      return hay.indexOf(filter) >= 0;
    });
    $("cohort-count2").textContent = cohortCache.length
      ? "(" + rows.length + " из " + cohortCache.length + ")"
      : "";
    tb.innerHTML = rows.length ? "" : "<tr><td colspan='7' class='sb-dim'>Никого не найдено.</td></tr>";
    rows.forEach(function (p) {
      var i = cohortCache.indexOf(p);
      var delta = (p.gad7_last !== null && p.gad7_first !== null) ? p.gad7_last - p.gad7_first : null;
      var name = (p.label || "П-" + (p.patient_hash || "").slice(0, 6));
      var cl = (p.cluster || "").slice(0, 2);
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + name + "</td>" +
        "<td>" + (cl ? "<b>" + cl + "</b>" + (p.ood ? " <span title='Махаланобис OOD: вне границ кластера' style='color:#c0392b'>⊙OOD</span>" : "") : "<span class='sb-dim'>—</span>") + "</td>" +
        "<td>" + (p.gad7_first === null ? "—" : p.gad7_first) + " → <b>" + (p.gad7_last === null ? "—" : p.gad7_last) + "</b></td>" +
        "<td>" + (delta === null ? "—" : (delta > 0 ? "+" : "") + delta) + "</td>" +
        "<td>" + (p.doses_logged || 0) + "</td>" +
        "<td>" + (p.doses_skipped || 0) + "</td>" +
        "<td>" +
          "<button type='button' class='cp-btn cp-btn-sm' data-act='hist' data-idx='" + i + "'>История</button> " +
          "<button type='button' class='cp-btn cp-btn-outline cp-btn-sm' data-act='qr' data-idx='" + i + "'>QR</button> " +
          "<button type='button' class='cp-btn cp-btn-outline cp-btn-sm' data-act='demo' data-idx='" + i + "'>Войти</button>" +
        "</td>";
      tr.querySelectorAll("button[data-act]").forEach(function (b) {
        b.addEventListener("click", function () {
          if (b.dataset.act === "hist") {
            openPatientTimeline(p.patient_hash, name);
          } else if (b.dataset.act === "qr") {
            showDemoQR(p.patient_hash, name);
          } else {
            demoPatientLogin(p.patient_hash);
          }
        });
      });
      tb.appendChild(tr);
    });
  }

  function showDemoQR(hash, label) {
    call("/api/v1/auth/demo-patient-qr?patient_hash=" + hash)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        $("demoqr-img").innerHTML =
          "<img src='data:image/png;base64," + d.qr_png_base64 + "' width='240' height='240' " +
          "alt='QR входа " + (label || "") + "'>";
        $("modal-demoqr").hidden = false;
      });
  }

  function demoPatientLogin(hash) {
    call("/api/v1/auth/demo-patient-login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_hash: hash }),
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (res.ok) {
          window.open("/static/pwa/index.html?demo=" + res.d.access_token, "_blank");
        } else {
          alert("Демо-вход не удался: " + (res.d.detail || ""));
        }
      }).catch(function (e) { alert("Сеть: " + e.message); });
  }

  /* Таймлайн пациента в дашборде по клику из когорты (и по алерту) */
  var dashPatientHash = null, dashPatientLabel = "";
  function openPatientTimeline(hash, label) {
    dashPatientHash = hash; dashPatientLabel = label || "";
    // записка пациента живёт в панели: прячем инструкцию, показываем контент
    var empty = $("sb-empty"), content = $("sb-content");
    if (empty && content) { empty.hidden = true; content.hidden = false; }
    renderPatientNote(hash, label);
    renderDash();
    $("dash").hidden = false;
  }

  /* ==== «Что изменилось с прошлого визита» — записка врача (панель + дашборд) ====
   * Данные: /patient/{hash}/timeline (RLS). Никаких генеративных резюме:
   * только факты из БД с датами — динамика шкал, НЯ, приёмы, стрик. */
  function renderPatientNote(hash, label) {
    var box = $("note-change-body");
    if (!box) return;
    box.innerHTML = "<span class='sb-dim'>Загружаю историю…</span>";
    call("/api/v1/compass-bio/patient/" + hash + "/timeline")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (tl) {
        if (!tl) return;
        renderPatientNoteInto($("note-change-body"), $("gad-chart"), $("gad-chart-note"), tl, label);
        // дашборд-версия той же записки
        renderPatientNoteInto($("dash-facts"), $("dash-gad-chart"), $("dash-gad-note"), tl, label);
        // НЯ-кнопки: панель + дашборд
        var aes = tl.adverse_events || [];
        var pb = $("btn-ae-1071-p"), row = $("dash-ae-row");
        if (pb) pb.hidden = aes.length === 0;
        if (pb) pb.dataset.ph = hash;
        if (row) { row.hidden = aes.length === 0; row.dataset.ph = hash; }
        renderMsgThreadFor($("dash-msg-thread"), tl);
        renderMsgThreadFor($("tab-msg-thread"), tl);
      }).catch(function () {
        box.innerHTML = "<span class='sb-dim'>История недоступна (сеть).</span>";
      });
  }

  function renderPatientNoteInto(factsBox, canvas, noteEl, tl, label) {
    if (!factsBox) return;
    var bits = [];
    // 1. Динамика шкал: последние две точки каждой шкалы
    var byScale = {};
    (tl.scales || []).forEach(function (sc) {
      if (sc.scale_code === "GAD-7" || sc.scale_code === "HADS-A" || sc.scale_code === "HADS-D" ||
          sc.scale_code === "HAM-A" || sc.scale_code === "CGI") {
        (byScale[sc.scale_code] = byScale[sc.scale_code] || []).push(sc);
      }
    });
    var SCALE_RU = { "GAD-7": "Тревога GAD-7", "HADS-A": "HADS-тревога", "HADS-D": "HADS-депрессия",
                     "HAM-A": "HAM-A (врач)", "CGI": "CGI (врач)" };
    Object.keys(byScale).forEach(function (code) {
      // timeline приходит newest-first (ORDER BY taken_at DESC) — разворачиваем
      // в хронологию, иначе улучшение показывается как ухудшение
      var arr = byScale[code].slice().sort(function (a, b) {
        return new Date(a.taken_at) - new Date(b.taken_at);
      }).slice(-2);
      if (arr.length >= 2) {
        var d0 = new Date(arr[0].taken_at).toLocaleDateString("ru-RU");
        var d1 = new Date(arr[1].taken_at).toLocaleDateString("ru-RU");
        var delta = arr[1].total_score - arr[0].total_score;
        var arrow = delta < 0 ? "↓" : delta > 0 ? "↑" : "→";
        bits.push("<div class='note-line'><b>" + (SCALE_RU[code] || code) + "</b> " +
          arr[0].total_score + " → <b class='" + (delta < 0 ? "imp" : delta > 0 ? "worse" : "") + "'>" +
          arr[1].total_score + "</b> " + arrow + Math.abs(delta) +
          " <span class='when'>" + d0 + " → " + d1 + "</span></div>");
      } else if (arr.length === 1) {
        bits.push("<div class='note-line'><b>" + (SCALE_RU[code] || code) + "</b> " + arr[0].total_score +
          " <span class='when'>" + new Date(arr[0].taken_at).toLocaleDateString("ru-RU") + "</span></div>");
      }
    });
    // 2. Приёмы за 7 дней + стрик
    var adh = (tl.adherence || []).slice(0, 7);
    if (adh.length) {
      var taken = adh.filter(function (a) { return !a.dose_skipped; }).length;
      var skips = adh.length - taken;
      var times = adh.filter(function (a) { return a.intake_time; }).map(function (a) { return a.intake_time; });
      bits.push("<div class='note-line'>Приёмы (7 дн.): <b>" + taken + "/" + adh.length + "</b>" +
        (skips ? " · пропущено " + skips : " · без пропусков") +
        (times.length ? " · приём в " + times[0] : "") + "</div>");
    }
    // 3. Жалобы/НЯ за период
    var aes = (tl.adverse_events || []).slice(0, 4);
    aes.forEach(function (a) {
      bits.push("<div class='note-line'>НЯ: <b>" + a.meddra_term + "</b> CTCAE " + a.severity_ctcae +
        " <span class='when'>" + new Date(a.reported_at).toLocaleDateString("ru-RU") +
        (a.has_draft ? " · черновик 1071 готов" : "") + "</span></div>");
    });
    // 4. Жалобы словами из приёмов
    var notes = (tl.adherence || []).filter(function (a) { return a.notes; }).slice(0, 3);
    notes.forEach(function (a) {
      bits.push("<div class='note-line'>Жалоба: «" + String(a.notes).replace(/[<>]/g, "") +
        "» <span class='when'>" + new Date(a.taken_at).toLocaleDateString("ru-RU") + "</span></div>");
    });
    factsBox.innerHTML = bits.length ? bits.join("") :
      "<span class='sb-dim'>Данных пока нет: пациент не отмечал приёмы и не заполнял шкалы.</span>";
    // график GAD-7 с линиями КР
    var gad = (byScale["GAD-7"] || []).slice().sort(function (a, b) {
      return new Date(a.taken_at) - new Date(b.taken_at);
    }).slice(-10);
    if (canvas && gad.length >= 2) {
      canvas.hidden = false;
      if (noteEl) noteEl.hidden = false;
      drawGadTrend(canvas, gad.map(function (g) {
        return { v: g.total_score, d: new Date(g.taken_at).toLocaleDateString("ru-RU") };
      }));
    } else if (canvas) {
      canvas.hidden = true;
      if (noteEl) noteEl.hidden = true;
    }
  }

  /* График GAD-7: точки + линии КР МЗ РФ (ответ −50% от исходного, ремиссия <5) */
  function drawGadTrend(cv, pts) {
    var ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height, padL = 26, padB = 16, padT = 8;
    ctx.clearRect(0, 0, W, H);
    var maxV = 21;
    var yOf = function (v) { return padT + (H - padT - padB) * (1 - v / maxV); };
    var xOf = function (i) { return padL + (W - padL - 8) * (i / Math.max(1, pts.length - 1)); };
    ctx.strokeStyle = "#e4e7eb"; ctx.font = "9px system-ui"; ctx.fillStyle = "#6b7280";
    for (var k = 0; k <= 3; k++) {
      var y = padT + (H - padT - padB) * k / 3;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - 8, y); ctx.stroke();
      ctx.fillText(String(Math.round(maxV * (1 - k / 3))), 4, y + 3);
    }
    ctx.save(); ctx.setLineDash([4, 4]);
    var base = pts[0].v;
    if (base > 0) {
      var yr = yOf(Math.max(0, base * 0.5));
      ctx.strokeStyle = "#d97706"; ctx.beginPath(); ctx.moveTo(padL, yr); ctx.lineTo(W - 8, yr); ctx.stroke();
      ctx.fillStyle = "#d97706"; ctx.fillText("ответ −50%", padL + 4, yr - 3);
    }
    var yrem = yOf(5);
    ctx.strokeStyle = "#1f8a4c"; ctx.beginPath(); ctx.moveTo(padL, yrem); ctx.lineTo(W - 8, yrem); ctx.stroke();
    ctx.fillStyle = "#1f8a4c"; ctx.fillText("ремиссия <5", padL + 4, yrem - 3);
    ctx.restore();
    ctx.strokeStyle = "#161616"; ctx.lineWidth = 1.8; ctx.beginPath();
    pts.forEach(function (p, i) { i ? ctx.lineTo(xOf(i), yOf(p.v)) : ctx.moveTo(xOf(i), yOf(p.v)); });
    ctx.stroke();
    ctx.fillStyle = "#161616";
    pts.forEach(function (p, i) {
      ctx.beginPath(); ctx.arc(xOf(i), yOf(p.v), 3, 0, 7); ctx.fill();
    });
    ctx.font = "9px system-ui"; ctx.fillStyle = "#6b7280";
    ctx.fillText(pts[0].d, padL, H - 4);
    var last = pts[pts.length - 1].d;
    ctx.textAlign = "right"; ctx.fillText(last, W - 8, H - 4); ctx.textAlign = "left";
  }

  /* ==== HAM-A-14: шкала Гамильтона, заполняет ВРАЧ (Приложение КР МЗ РФ) ====
   * 14 пунктов 0–4, автосумма, факторы: психическая (1–6, 14) / соматическая (7–13).
   * Пороги КР: ≥14 — клинически значимая тревога; ответ = редукция ≥50%; ремиссия ≤7. */
  var HAMA_ITEMS = [
    "Тревожное настроение", "Напряжение", "Страхи", "Бессонница",
    "Когнитивные затруднения", "Депрессивное настроение",
    "Соматические мышечные", "Соматические сенсорные", "Сердечно-сосудистые",
    "Респираторные", "Желудочно-кишечные", "Мочеполовые", "Вегетативные",
    "Поведение при осмотре",
  ];
  var HAMA_CHIP_LABEL = ["0 нет", "1 лёгкая", "2 умерен.", "3 знач.", "4 макс."];
  var hamaAnswers = [];

  function renderHamA(ph) {
    hamaAnswers = new Array(HAMA_ITEMS.length).fill(null);
    var box = $("hama-items");
    if (!box) return;
    box.innerHTML = "";
    var head = document.createElement("div");
    head.className = "hama-head";
    head.innerHTML = "<span>0</span><span>1</span><span>2</span><span>3</span><span>4</span>";
    box.appendChild(head);
    HAMA_ITEMS.forEach(function (txt, i) {
      var row = document.createElement("div");
      row.className = "hama-row";
      var t = document.createElement("div");
      t.className = "hama-row__text";
      t.textContent = (i + 1) + ". " + txt;
      row.appendChild(t);
      var chips = document.createElement("div");
      chips.className = "hama-row__chips";
      HAMA_CHIP_LABEL.forEach(function (lbl, v) {
        var c = document.createElement("button");
        c.type = "button"; c.className = "hama-chip"; c.textContent = String(v); c.title = lbl;
        c.addEventListener("click", function () {
          hamaAnswers[i] = v;
          Array.from(chips.children).forEach(function (x, j) {
            x.classList.toggle("hama-chip--on", j === v);
          });
          updateHamASum();
        });
        chips.appendChild(c);
      });
      row.appendChild(chips);
      box.appendChild(row);
    });
    updateHamASum();
    var note = $("hama-saved-note");
    if (note) note.textContent = "";
  }

  function updateHamASum() {
    var filled = hamaAnswers.filter(function (x) { return x !== null; }).length;
    var sumEl = $("hama-sum"), btn = $("btn-hama-save");
    if (btn) btn.disabled = filled < HAMA_ITEMS.length;
    if (!sumEl) return;
    if (filled < HAMA_ITEMS.length) {
      sumEl.innerHTML = "<span class='sb-dim'>Заполнено " + filled + " из 14. Шкалу записывают целиком — пропуски искажают порог ≥14.</span>";
      return;
    }
    var total = hamaAnswers.reduce(function (a, b) { return a + b; }, 0);
    var psy = [0, 1, 2, 3, 4, 5, 13].reduce(function (a, i) { return a + hamaAnswers[i]; }, 0);
    var som = total - psy;
    var band = total < 14 ? "тревога не выявляется (<14)"
      : total < 28 ? "клинически значимая тревога (14–27)" : "тяжёлая тревога (≥28)";
    sumEl.innerHTML = "<b>Сумма " + total + "/56</b> · " + band +
      "<br>Психический фактор: <b>" + psy + "</b> · Соматический: <b>" + som + "</b>" +
      "<br><span class='sb-dim'>Пороги КР МЗ РФ: ГТР ≥14 · ответ = редукция ≥50% · ремиссия ≤7</span>";
  }

  var hamaSaveBtn = $("btn-hama-save");
  if (hamaSaveBtn) {
    hamaSaveBtn.addEventListener("click", function () {
      var ph = hamaSaveBtn.closest(".dash-card").dataset.ph;
      if (!ph || hamaAnswers.some(function (x) { return x === null; })) return;
      hamaSaveBtn.disabled = true;
      call("/api/v1/compass-bio/patient/" + ph + "/clinician-scale?code=HAM-A", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers: hamaAnswers }),
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          hamaSaveBtn.disabled = false;
          var note = $("hama-saved-note");
          if (res.ok) {
            var d = res.d;
            if (note) note.textContent = "Записано: HAM-A = " + d.total_score + " (" + (d.severity || "") + ")";
            refreshAlerts();
          } else if (note) {
            note.textContent = "Ошибка: " + (res.d.detail || "не записано");
          }
        }).catch(function (e) {
          hamaSaveBtn.disabled = false;
          var note = $("hama-saved-note");
          if (note) note.textContent = "Ошибка сети: " + e.message;
        });
    });
  }

  /* ==== Черновик извещения о НЯ (Приказ 1071) — формирует сервер, решает врач ==== */
  function requestDraft1071(ph, done) {
    call("/api/v1/compass-bio/patient/" + ph + "/adverse-events/draft-1071", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) { done(res); })
      .catch(function (e) { done({ ok: false, d: { detail: "сеть: " + e.message } }); });
  }
  document.querySelectorAll("#btn-ae-1071, .js-1071").forEach(function (aeBtn) {
    aeBtn.addEventListener("click", function () {
      var ph = aeBtn.dataset.ph || ($("dash-ae-row") && $("dash-ae-row").dataset.ph);
      if (!ph) return;
      aeBtn.disabled = true;
      requestDraft1071(ph, function (res) {
        aeBtn.disabled = false;
        if (res.ok) {
          $("ae1071-body").textContent = res.d.draft_text;
          $("modal-1071").hidden = false;
          var note = $("ae-note");
          if (note) note.textContent = "Черновиков сформировано: " + res.d.drafts_formed;
          renderDash();
        } else {
          var note2 = $("ae-note-panel") || $("ae-note");
          if (note2) { note2.hidden = false; note2.textContent = "Ошибка: " + (res.d.detail || "не сформировано"); }
        }
      });
    });
  });
  /* Ответ пациенту: общая функция (дашборд + вкладка «Сообщения») */
  function replyToPatient(ph, text, noteEl, btn) {
    if (!ph) { noteEl.textContent = "Сначала выберите пациента."; return; }
    if (text.length < 2) { noteEl.textContent = "Пусто."; return; }
    btn.disabled = true;
    call("/api/v1/compass-bio/patient/" + ph + "/messages", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: text }),
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok) {
          noteEl.textContent = "Отправлено — появится в PWA пациента.";
          renderPatientNote(ph, dashPatientLabel);
          renderDash();
        } else noteEl.textContent = "Ошибка: " + (res.d.detail || "");
      }).catch(function (e) { btn.disabled = false; noteEl.textContent = "Сеть: " + e.message; });
  }
  var msgReplyBtn = $("btn-msg-reply");
  if (msgReplyBtn) {
    msgReplyBtn.addEventListener("click", function () {
      var ph = dashPatientHash || (state.currentAlert && state.currentAlert.patient_hash);
      replyToPatient(ph, $("msg-reply").value.trim(), $("msg-reply-note"), msgReplyBtn);
    });
  }

  /* ==== Осознанное несогласие → audit_log (против иллюзии понимания) ==== */
  document.querySelectorAll(".js-disagree, #btn-disagree").forEach(function (disBtn) {
    disBtn.addEventListener("click", function () { $("modal-disagree").hidden = false; });
  });
  var mDis = $("modal-disagree");
  if (mDis) {
    $("btn-disagree-cancel").addEventListener("click", function () { mDis.hidden = true; });
    mDis.addEventListener("click", function (e) { if (e.target === mDis) mDis.hidden = true; });
    $("btn-disagree-ok").addEventListener("click", function () {
      var ph = dashPatientHash || (state.currentAlert && state.currentAlert.patient_hash);
      var note = $("disagree-action").value.trim();
      var notes = document.querySelectorAll("#disagree-note, #disagree-note-panel");
      if (!ph) { notes.forEach(function (n) { n.textContent = "Сначала выберите пациента."; }); return; }
      call("/api/v1/compass-bio/patient/" + ph + "/disagree", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: note }),
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          mDis.hidden = true;
          notes.forEach(function (n) { n.textContent = res.ok ? "Записано в аудит-лог." : ("Ошибка: " + (res.d.detail || "")); });
        }).catch(function (e) { notes.forEach(function (n) { n.textContent = "Ошибка сети: " + e.message; }); });
    });
  }

  /* ==== Провенанс: реестр источников — клик по цифре → карточка ==== */
  document.querySelectorAll(".js-sources").forEach(function (srcBtn) {
    srcBtn.addEventListener("click", function () {
      call("/api/v1/compass-bio/sources")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          var box = $("sources-body");
          box.innerHTML = (d.sources || []).map(function (x) {
            return "<div class='dash-tl'><b>" + x.doc_title + "</b> <span class='when'>" +
              x.doc_ref + (x.verified_url ? " · <a href='" + x.verified_url +
              "' target='_blank' rel='noopener'>оригинал</a>" : " · оригинал не открывался") +
              "</span><br><span class='sb-dim'>" + (x.note || "") + "</span></div>";
          }).join("") ||
            "<span class='sb-dim'>Реестр пуст.</span>";
          $("modal-sources").hidden = false;
        });
    });
  });
  var mQR = $("modal-demoqr");
  if (mQR) {
    $("btn-demoqr-close").addEventListener("click", function () { mQR.hidden = true; });
    mQR.addEventListener("click", function (e) { if (e.target === mQR) mQR.hidden = true; });
  }
  var mSrc = $("modal-sources");
  if (mSrc) {
    $("btn-sources-close").addEventListener("click", function () { mSrc.hidden = true; });
    mSrc.addEventListener("click", function (e) { if (e.target === mSrc) mSrc.hidden = true; });
  }

  var m1071 = $("modal-1071");
  if (m1071) {
    $("btn-1071-close").addEventListener("click", function () { m1071.hidden = true; });
    $("btn-1071-print").addEventListener("click", function () { window.print(); });
    m1071.addEventListener("click", function (e) { if (e.target === m1071) m1071.hidden = true; });
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

  /* ==== Вкладка «Сообщения»: выбор пациента + тред + ответ ==== */
  function renderMsgPatientSelect(patients) {
    var sel = $("msg-patient");
    if (!sel) return;
    var cur = sel.value;
    sel.innerHTML = "<option value=''>— выберите пациента —</option>" +
      patients.map(function (p) {
        return "<option value='" + p.patient_hash + "'>" +
          (p.label || "П-" + (p.patient_hash || "").slice(0, 6)) + "</option>";
      }).join("");
    if (cur) sel.value = cur;
  }
  function renderMsgThreadFor(box, tl) {
    if (!box) return;
    var msgs = (tl.messages || []).slice().reverse();
    box.innerHTML = msgs.length ? msgs.map(function (m) {
      return "<div class='dash-tl" + (m.sender_role === 'DOCTOR' ? " msg-doc" : "") + "'>" +
        (m.sender_role === 'PATIENT' ? "<b>Пациент:</b> " : "<b>Врач:</b> ") +
        String(m.body).replace(/[<>]/g, "") +
        "<span class='when'>" + new Date(m.created_at).toLocaleString("ru-RU") + "</span></div>";
    }).join("") : "<span class='sb-dim'>Сообщений пока нет.</span>";
  }
  var msgSel = $("msg-patient");
  if (msgSel) {
    msgSel.addEventListener("change", function () {
      var ph = msgSel.value;
      var thread = $("tab-msg-thread");
      var reply = $("tab-msg-reply"), btn = $("btn-tab-msg-reply");
      if (!ph) {
        thread.innerHTML = "<span class='sb-dim'>Выберите пациента: здесь его тред из PWA.</span>";
        reply.disabled = btn.disabled = true;
        return;
      }
      reply.disabled = btn.disabled = false;
      dashPatientHash = ph;
      thread.innerHTML = "<span class='sb-dim'>Загружаю…</span>";
      call("/api/v1/compass-bio/patient/" + ph + "/timeline")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (tl) {
          if (!tl) return;
          renderMsgThreadFor(thread, tl);
          renderMsgThreadFor($("dash-msg-thread"), tl);
        });
    });
  }
  var tabMsgBtn = $("btn-tab-msg-reply");
  if (tabMsgBtn) {
    tabMsgBtn.addEventListener("click", function () {
      replyToPatient($("msg-patient").value, $("tab-msg-reply").value.trim(),
                     $("tab-msg-note"), tabMsgBtn);
    });
  }

  /* Когорта терапии: живые данные из БД (RLS врача) */
  function loadCohort() {
    call("/api/v1/auth/my-patients").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var cc = $("cohort-count");
        if (cc) cc.textContent = d.count ? "(" + d.count + ")" : "";
        renderMsgPatientSelect(d.patients || []);
      });
  }

  function telemetry() {
    call("/api/v1/telemetry/summary").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (t) {
        if (!t) return;
        var line = "p95 = " + (t.p95_ms === null ? "—" : t.p95_ms.toFixed(1) + " мс") +
          " • p99 = " + (t.p99_ms === null ? "—" : t.p99_ms.toFixed(1) + " мс") +
          " • запросов: " + t.requests_observed;
        var el = $("sys-sla");
        if (el) el.textContent = line;
        var old = $("sb-telemetry");
        if (old) old.textContent = "телеметрия: " + line;
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
  /* Категории маршрутизации (Условие кейса: «обязательно» и «желательно» различимы):
   * бейдж с цветом ОБЯЗАТЕЛЬНО = красный, ЖЕЛАТЕЛЬНО = янтарный, первичное звено = серый. */
  var ROUTE_RU = {
    MANDATORY_PSYCHIATRIST: ["ОБЯЗАТЕЛЬНО к психиатру", "route-badge route--mandatory"],
    RECOMMENDED_PSYCHIATRIST: ["ЖЕЛАТЕЛЬНО к психиатру", "route-badge route--recommended"],
    PRIMARY_CARE_MANAGEMENT: ["Первичное звено", "route-badge route--primary"],
  };
  function routeBadge(cat) {
    var m = ROUTE_RU[cat];
    if (!m) return "<b>" + String(cat || "") + "</b>";
    return "<span class='" + m[1] + "'>" + m[0] + "</span>";
  }

  function refreshAlerts() {
    call("/api/v1/alerts/unconfirmed").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var a = d.alerts && d.alerts[0];
        // Alert fatigue (Ancker 2017): один и тот же алерт не пингуем чаще,
        // чем раз в 5 минут — баннер обновляется тихо, без визуального «дёргания»
        var now = Date.now();
        var lastPing = state.alertPingAt && state.alertPingAt[a ? a.alert_id : 0] || 0;
        var quiet = a && state.currentAlert && state.currentAlert.alert_id === a.alert_id &&
                    (now - lastPing) < 5 * 60 * 1000;
        $("sb-alert").hidden = !a;
        if (a) {
          state.currentAlert = a;
          if (!quiet) {
            state.alertPingAt = state.alertPingAt || {};
            state.alertPingAt[a.alert_id] = now;
            $("sb-alert-title").innerHTML = "Критический алерт (" + d.count + ") " + routeBadge(a.category);
            $("sb-alert-sub").textContent = (a.reasons || []).join("; ") +
              " • " + new Date(a.created_at).toLocaleTimeString("ru-RU");
          }
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
    $("confirm-info").innerHTML = "Алерт " + a.alert_id + " • " + routeBadge(a.category) + " • " + (a.reasons || []).join("; ");
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
          div.innerHTML = routeBadge(a.category) + (a.reasons || []).join("; ") +
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
    // HAM-A-14 появляется только когда выбран конкретный пациент
    var hamaCard = $("dash-hama-card");
    if (hamaCard) {
      hamaCard.hidden = !alertPh;
      if (alertPh && hamaCard.dataset.ph !== alertPh) {
        hamaCard.dataset.ph = alertPh;
        hamaCard.dataset.answers = "";
        renderHamA(alertPh);
      }
    }
    if (alertPh) {
      // записка «Что изменилось» рендерится одной функцией для панели и дашборда
      renderPatientNote(alertPh, dashPatientLabel);
    } else if (tlBox) {
      tlBox.innerHTML = "";
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
      sv.className = "sb-svetofor dash-sv hero-verdict hero-" + (stMap[d.overall_status] || "amber").replace("green", "green");
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
      bindClusterCanvasEvents();
      if (clu && clu.illustrative_2d_projection) {
        drawClusterMap(clu);
        $("dash-cluster-kv").innerHTML =
          "<div><b>Кластер:</b> " + (clu.assigned_cluster_title || "—") + "</div>" +
          "<div><b>Рекомендация:</b> " + (clu.recommended_control_action || "—") + "</div>" +
          "<div><b>DTW до траектории Сеченова (36 нед)</b> " + (clu.dtw_distance_to_sechenov_rki || "—") + "</div>" +
          "<div><b>Бенчмарк:</b> " + (clu.clinical_benchmark || "—") + "</div>" +
          (clu.clinical_use ? "<div class='sb-dim'>" + clu.clinical_use + "</div>" : "");
      }
    } else if (alertPh) {
      // Пациент из БД (без разбора ЭМК): карта строится из cluster-map API,
      // KV — из персонального кластер-профиля (24D, OOD, приверженность)
      bindClusterCanvasEvents();
      drawClusterMap(null);
      call("/api/v1/compass-bio/patient/" + alertPh + "/cluster-profile", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (cp) {
          if (!cp) return;
          var cur = dashPatientHash || (state.currentAlert && state.currentAlert.patient_hash);
          if (cur !== alertPh) return;
          var ood = cp.ood || {};
          var tops = (ood.top_contributions || []).slice(0, 2)
            .map(function (c) { return c[0]; }).join(", ");
          $("dash-cluster-kv").dataset.ph = alertPh;
          $("dash-cluster-kv").innerHTML =
            "<div><b>Кластер:</b> " + (cp.cluster_title || cp.cluster_code) + "</div>" +
            "<div><b>DTW до траектории Сеченова (36 нед)</b> " + (cp.dtw || "—") + "</div>" +
            "<div><b>Махаланобис D²:</b> " + (ood.mahalanobis_dist !== undefined && ood.mahalanobis_dist !== null ? Number(ood.mahalanobis_dist).toFixed(1) : "—") +
            " • OOD: " + (ood.is_ood_anomaly ? "<b style='color:#c0392b'>вне границ — экстраполяции нет</b>" : "в коридоре кластера") +
            (tops ? " • вклад: " + tops : "") + "</div>" +
            "<div><b>Приверженность (PDC):</b> " + ((cp.facts || {}).pdc || "—") + "% • приёмов: " +
            ((cp.facts || {}).doses_logged || 0) + " • пропусков: " + ((cp.facts || {}).skips || 0) + "</div>" +
            ((cp.imputed_inputs || []).length ? "<div class='sb-dim'>Оси по умолчанию (нет данных): " + cp.imputed_inputs.join(", ") + "</div>" : "");
        }).catch(function () {});
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
          div.innerHTML = routeBadge(a.category) + (a.reasons || []).join("; ") +
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

  /* ---------- интерактивная кластер-карта (pan/zoom/drag/click) ---------- */
  var clusterMap = { data: null, vx: 0, vy: 0, scale: 1, dragNode: null, dragBg: null, hover: null };
  var CLUSTER_COLORS = { K1: "#4a7c59", K2: "#b7791f", K3: "#2b6cb0", K4: "#805ad5", K5: "#c0392b" };

  function loadClusterMap() {
    call("/api/v1/compass-bio/cluster-map").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        clusterMap.data = d;
        clusterMap.vx = 0; clusterMap.vy = 0; clusterMap.scale = 1;
        renderDash(); // перерисовать дашборд с картой
      });
  }

  function drawClusterMap(cl) {
    var cv = $("dash-cluster"); if (!cv) return;
    var ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    var d = clusterMap.data;
    if (!d) {
      ctx.fillStyle = "#8d8d8d"; ctx.font = "12px system-ui";
      ctx.fillText("Карта загружается…", 20, H / 2);
      // триггер загрузки один раз
      if (!clusterMap.loading) { clusterMap.loading = true; loadClusterMap(); }
      return;
    }
    var cx = W / 2 + clusterMap.vx, cy = H / 2 + clusterMap.vy;
    var sc = clusterMap.scale * Math.min(W, H) / 9.2;
    function P(pt) { return [cx + pt[0] * sc, cy - pt[1] * sc]; }
    var el = d.mahalanobis_ellipse;
    ctx.setLineDash([7, 6]); ctx.strokeStyle = "#7c8b9a"; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.ellipse(cx, cy, el.rx * sc, el.ry * sc, 0, 0, Math.PI * 2); ctx.stroke();
    ctx.setLineDash([]);
    // референсные точки (честно помечены)
    ctx.fillStyle = "rgba(110,143,134,.45)";
    d.reference_points.forEach(function (p) {
      var pt = P([p.x, p.y]);
      ctx.beginPath(); ctx.arc(pt[0], pt[1], 3.2, 0, 7); ctx.fill();
    });
    // подписи кластеров
    ctx.font = "600 12px system-ui";
    d.reference_labels.forEach(function (l) {
      var pt = P([l.cx, l.cy]);
      ctx.fillStyle = "#4a5d68";
      ctx.fillText(l.label, pt[0] - 22, pt[1] + 24);
    });
    // пациенты (реальные). Подпись — только при зуме ≥ 1.6: 105 подписей
    // без зума превращаются в чёрное пятно (было на скрине жюри)
    d.patients.forEach(function (p) {
      var pt = P([p.x, p.y]);
      var col = CLUSTER_COLORS[(p.cluster || "").split("_")[0]] || "#15171a";
      ctx.beginPath(); ctx.arc(pt[0], pt[1], p.ood ? 6 : 5.5, 0, 7);
      ctx.fillStyle = col; ctx.fill();
      if (p.ood) { ctx.strokeStyle = "#c0392b"; ctx.lineWidth = 2; ctx.stroke(); }
      if (clusterMap.scale >= 1.6) {
        ctx.font = "700 11px system-ui"; ctx.fillStyle = "#15171a";
        ctx.fillText("П-" + p.hash6, pt[0] + 8, pt[1] - 8);
      }
    });
    // легенда
    ctx.fillStyle = "#525252"; ctx.font = "11px system-ui";
    ctx.fillText("серые точки — референсные траектории исследований (не пациенты)", 12, 18);
    ctx.fillText("цветные — ваши пациенты (RLS); красный контур = OOD, без экстраполяции", 12, 34);
    ctx.fillText("колесо — зум (подписи пациентов от 1.6x) • перетаскивание — панорама • клик — карточка", 12, H - 10);
  }

  function bindClusterCanvasEvents() {
    var cv = document.getElementById("dash-cluster");
    if (!cv || cv.__bound) return;
    cv.__bound = true;
    var toWorld = function (px, py) {
      var W = cv.width, H = cv.height;
      var sc = clusterMap.scale * Math.min(W, H) / 9.2;
      return [(px - W / 2 - clusterMap.vx) / sc, -(py - H / 2 - clusterMap.vy) / sc];
    };
    cv.addEventListener("wheel", function (e) {
      e.preventDefault();
      clusterMap.scale = Math.min(4, Math.max(0.5, clusterMap.scale * (e.deltaY < 0 ? 1.15 : 0.87)));
      renderDash();
    }, { passive: false });
    cv.addEventListener("mousedown", function (e) {
      var rect = cv.getBoundingClientRect();
      var px = (e.clientX - rect.left) * (cv.width / rect.width);
      var py = (e.clientY - rect.top) * (cv.height / rect.height);
      // попадание в узел пациента?
      var hit = null;
      if (clusterMap.data) {
        var sc = clusterMap.scale * Math.min(cv.width, cv.height) / 9.2;
        clusterMap.data.patients.forEach(function (p) {
          var nx = cv.width / 2 + clusterMap.vx + p.x * sc;
          var ny = cv.height / 2 + clusterMap.vy - p.y * sc;
          if (Math.hypot(px - nx, py - ny) < 12) hit = p;
        });
      }
      if (hit) { clusterMap.dragNode = hit; }
      else { clusterMap.dragBg = { x: e.clientX, y: e.clientY }; }
    });
    cv.addEventListener("mousemove", function (e) {
      var rect = cv.getBoundingClientRect();
      var px = (e.clientX - rect.left) * (cv.width / rect.width);
      var py = (e.clientY - rect.top) * (cv.height / rect.height);
      if (clusterMap.dragNode) {
        var sc = clusterMap.scale * Math.min(cv.width, cv.height) / 9.2;
        clusterMap.dragNode.x = (px - cv.width / 2 - clusterMap.vx) / sc;
        clusterMap.dragNode.y = -(py - cv.height / 2 - clusterMap.vy) / sc;
        renderDash();
      } else if (clusterMap.dragBg) {
        clusterMap.vx += e.clientX - clusterMap.dragBg.x;
        clusterMap.vy += e.clientY - clusterMap.dragBg.y;
        clusterMap.dragBg = { x: e.clientX, y: e.clientY };
        renderDash();
      }
    });
    window.addEventListener("mouseup", function () { clusterMap.dragNode = null; clusterMap.dragBg = null; });
    cv.addEventListener("click", function (e) {
      var rect = cv.getBoundingClientRect();
      var px = (e.clientX - rect.left) * (cv.width / rect.width);
      var py = (e.clientY - rect.top) * (cv.height / rect.height);
      if (!clusterMap.data) return;
      var sc = clusterMap.scale * Math.min(cv.width, cv.height) / 9.2;
      var hit = null;
      clusterMap.data.patients.forEach(function (p) {
        var nx = cv.width / 2 + clusterMap.vx + p.x * sc;
        var ny = cv.height / 2 + clusterMap.vy - p.y * sc;
        if (Math.hypot(px - nx, py - ny) < 14) hit = p;
      });
      if (hit) {
        var hashFull = (clusterMap.data.patients.indexOf(hit) >= 0) ? hit.hash6 : "";
        // полный hash ищем по префиксу из my-patients
        call("/api/v1/auth/my-patients").then(function (r) { return r.ok ? r.json() : null; })
          .then(function (d) {
            if (!d) return;
            var p = (d.patients || []).find(function (x) { return (x.patient_hash || "").indexOf(hit.hash6) === 0; });
            if (p) openPatientTimeline(p.patient_hash, "П-" + hit.hash6);
          });
      }
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
