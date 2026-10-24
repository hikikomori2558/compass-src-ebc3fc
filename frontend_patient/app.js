/*
 * frontend_patient/app.js — PWA-дневник пациента COMPASS.
 *
 * UX по docs/UX_UI_DESIGN.md: базовый чек-ин = 3 тапа (таблетка → настроение →
 * отправить), полный опросник GAD-7/PHQ-9 — пошагово тапами по валидированным
 * формулировкам, суицидальный скрининг — каскад C-SSRS; после отправки —
 * НЕМЕДЛЕННЫЙ фидбек (при критичном ответе — отдельный тёплый экран поддержки);
 * вкладка «Терапия по неделям» — ожидания/побочки и свободная жалоба врачу;
 * вкладка «Прогресс» — тренд GAD-7 и настроение по дням.
 *
 * Поток: QR → /api/v1/auth/bind → пациентский JWT → POST /api/v1/patient/checkin
 * (идемпотентный event_uuid + офлайн-очередь; при критичных ответах врачу уходит
 * ПУСТОЙ WS-пинг — клинический контент остаётся в контуре МО; Условие 3: система
 * не советует, только фиксирует и передаёт).
 */
(function () {
  "use strict";

  var QS = new URLSearchParams(window.location.search);
  // По умолчанию API — тот же origin (стенд: https://.../static/pwa/).
  // Явное ?api=... — для локальной разработки (python3 -m http.server 5501).
  var SAME_ORIGIN = (location.origin && location.origin.indexOf("http") === 0) ? location.origin : "http://127.0.0.1:8000";
  var CFG = {
    apiBase: (QS.get("api") || localStorage.getItem("compass_patient_api") || SAME_ORIGIN).replace(/\/+$/, "")
  };
  var STORE = {
    get session() {
      try { return JSON.parse(localStorage.getItem("compass_patient_session") || "null"); }
      catch (e) { return null; }
    },
    set session(s) { localStorage.setItem("compass_patient_session", JSON.stringify(s)); },
    clear: function () { localStorage.removeItem("compass_patient_session"); }
  };
  var QUEUE_KEY = "compass_patient_outbox";
  var LOG_KEY = "compass_patient_log";
  var MOOD_KEY = "compass_patient_moods";

  function el(id) { return document.getElementById(id); }
  function uuid() {
    return (crypto.randomUUID) ? crypto.randomUUID() :
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
  }
  function today() { return new Date().toISOString().slice(0, 10); }

  function api(path, opts, token, opts2) {
    opts = opts || {};
    var allowRebind = !(opts2 && opts2.queue); // 401 из офлайн-очереди не разлогинивает
    return fetch(CFG.apiBase + path, Object.assign({}, opts, {
      headers: Object.assign({ "Content-Type": "application/json" },
        token ? { "Authorization": "Bearer " + token } : {}, opts.headers || {}),
    })).then(function (r) {
      if (r.status === 401) {
        if (allowRebind) { STORE.clear(); showView("bind"); }
        throw new Error("Сессия истекла — привяжитесь заново");
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* ================= офлайн-очередь (Условие: PWA живёт в лифтах и метро) ==== */
  function loadQueue() { try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch (e) { return []; } }
  function saveQueue(q) { localStorage.setItem(QUEUE_KEY, JSON.stringify(q)); }
  function enqueue(item) { var q = loadQueue(); q.push(item); saveQueue(q); }
  function flushQueue() {
    var t = STORE.session; if (!t) return;
    var q = loadQueue(); if (!q.length) return;
    var rest = [];
    (function next() {
      if (!q.length) { saveQueue([]); renderHistory(); return; }
      var item = q.shift();
      // queue: true — 401 устаревшего queued-чек-ина выбрасывает ЭТОТ элемент,
      // а не разлогинивает врача-сессию пациента (иначе гонка с живым чек-ином)
      api("/api/v1/patient/checkin", { method: "POST", body: JSON.stringify(item.payload) }, t.access_token, { queue: true })
        .then(function () { next(); })
        .catch(function () { rest.push(item); rest = rest.concat(q); saveQueue(rest); renderHistory(); });
    })();
  }

  function loadLog() { try { return JSON.parse(localStorage.getItem(LOG_KEY) || "[]"); } catch (e) { return []; } }
  function pushLog(entry) { var l = loadLog(); l.unshift(entry); localStorage.setItem(LOG_KEY, JSON.stringify(l.slice(0, 60))); }
  function loadMoods() { try { return JSON.parse(localStorage.getItem(MOOD_KEY) || "[]"); } catch (e) { return []; } }
  function pushMood(m) {
    var l = loadMoods().filter(function (x) { return x.d !== today(); });
    l.push({ d: today(), m: m }); localStorage.setItem(MOOD_KEY, JSON.stringify(l.slice(-30)));
  }

  /* ================= навигация (вкладки = 1 клик) ================= */
  function showView(name) {
    ["bind", "home", "survey", "progress", "info"].forEach(function (v) {
      el("view-" + v).style.display = (v === name) ? "" : "none";
    });
    document.querySelectorAll(".navchip").forEach(function (c) {
      c.classList.toggle("navchip--active", c.dataset.go === name);
    });
    if (name === "progress") renderProgress();
    window.scrollTo(0, 0);
  }
  document.addEventListener("click", function (e) {
    var go = e.target.closest("[data-go]");
    if (go) { e.preventDefault(); showView(go.dataset.go); }
  });

  /* ================= привязка ================= */
  function bind(token) {
    var btn = el("btn-bind"); btn.disabled = true; btn.textContent = "Связываю…";
    el("bind-error").textContent = "";
    api("/api/v1/auth/bind", { method: "POST", body: JSON.stringify({ token: token }) })
      .then(function (t) {
        STORE.session = t;
        enterDiary();
      })
      .catch(function (err) {
        el("bind-error").textContent = "Не удалось привязаться: " + err.message +
          (err.stack ? " [" + String(err.stack).replace(/\s+/g, " ").slice(0, 300) + "]" : "");
      })
      .finally(function () { btn.disabled = false; btn.textContent = "Привязаться"; });
  }

  /* ================= чек-ин: 3 тапа ================= */
  var draft = { dose: null, mood: null, phq9: null, gad7: null, cssrs: null, item9: null };

  function resetDraft() {
    draft = { dose: null, mood: null, phq9: null, gad7: null, cssrs: null, item9: null };
    document.querySelectorAll(".choice").forEach(function (c) { c.classList.remove("choice--on"); });
    document.querySelectorAll(".mood").forEach(function (x) { x.classList.remove("mood--on"); });
    el("step-mood").hidden = true;
    el("step-send").hidden = true;
  }

  function goStep(n) {
    el("step-mood").hidden = !(n >= 2);
    el("step-send").hidden = !(n >= 3);
    if (n >= 3) {
      var parts = [];
      parts.push(draft.dose === "yes" ? "таблетка ✓" : "таблетка пропущена");
      if (draft.mood) parts.push("настроение " + draft.mood + "/5");
      el("send-summary").textContent = parts.join(" • ") +
        ". Полный опросник (2 недели) — кнопкой ниже: врачу важна динамика раз в 1–2 недели.";
    }
  }

  document.querySelectorAll(".choice").forEach(function (c) {
    c.addEventListener("click", function () {
      document.querySelectorAll(".choice").forEach(function (x) { x.classList.remove("choice--on"); });
      c.classList.add("choice--on");
      draft.dose = c.dataset.dose;
      goStep(2);
    });
  });
  document.querySelectorAll(".mood").forEach(function (c) {
    c.addEventListener("click", function () {
      document.querySelectorAll(".mood").forEach(function (x) { x.classList.remove("mood--on"); });
      c.classList.add("mood--on");
      draft.mood = parseInt(c.dataset.mood, 10);
      pushMood(draft.mood);
      goStep(3);
    });
  });

  /* ================= отправка + немедленный фидбек ================= */
  function buildPayload() {
    return {
      event_uuid: uuid(),
      phq9_total: (draft.phq9 && draft.phq9.total) || 0,
      phq9_item9: (draft.item9 !== null && draft.item9 !== undefined) ? draft.item9 : null,
      gad7_total: (draft.gad7 && draft.gad7.total) || 0,
      cssrs: (draft.cssrs !== null && draft.cssrs !== undefined) ? draft.cssrs : null,
      took_medication: draft.dose !== "no",
    };
  }

  function sendCheckin(payload) {
    var t = STORE.session;
    var btn = el("btn-checkin"); btn.disabled = true; btn.textContent = "Отправляю…";
    return api("/api/v1/patient/checkin", { method: "POST", body: JSON.stringify(payload) }, t.access_token)
      .then(function (resp) { return { resp: resp, offline: false }; })
      .catch(function (err) {
        var m = String(err.message);
        if (m.indexOf("HTTP") === 0 || m.indexOf("Failed to fetch") >= 0 || m.indexOf("NetworkError") >= 0) {
          enqueue({ queued_at: new Date().toISOString(), payload: payload });
          var critical = (payload.cssrs || 0) >= 1 || (payload.phq9_item9 || 0) >= 1;
          return { resp: { status: "QUEUED", critical: critical }, offline: true };
        }
        throw err;
      })
      .finally(function () { btn.disabled = false; btn.textContent = "Отправить врачу"; });
  }

  function showFeedback(resp, payload) {
    el("card-today").hidden = true;
    el("card-feedback").hidden = false;
    var ok = el("feedback-ok"), care = el("feedback-care");
    if (resp.critical) {
      ok.hidden = true; care.hidden = false;
    } else {
      care.hidden = true; ok.hidden = false;
      el("fb-main").textContent = resp.offline
        ? "Сохранено и уйдёт врачу, как только появится сеть."
        : "Врач увидит ваш ответ сегодня.";
      el("fb-insight").textContent = insightText(payload);
      var w = therapyWeek();
      el("fb-week").textContent = w ? ("Вы на " + w.week + "-й неделе из 24 — " + w.phase) : "Первый чек-ин — с него начинается статистика терапии.";
    }
    el("card-feedback").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function therapyWeek() {
    var log = loadLog();
    if (log.length < 2) return null;
    var first = new Date(log[log.length - 1].ts), now = new Date();
    var week = Math.max(1, Math.floor((now - first) / 6048e5) + 1);
    var phase = week <= 1 ? "первые дни — самочувствие может быть неровным, это ожидаемо" :
      week <= 4 ? "период, когда эффект обычно начинает проявляться" :
      week <= 12 ? "стабильный участок терапии" : "поддерживающий этап";
    return { week: Math.min(week, 24), phase: phase };
  }

  function insightText(payload) {
    var log = loadLog().filter(function (x) { return x.gad7 !== null && x.gad7 !== undefined; });
    if (log.length < 2 || !payload.gad7_total) return "";
    var prev = log[0].gad7, cur = payload.gad7_total;
    if (cur < prev) return "Тревога по опроснику ниже, чем в прошлый раз (" + prev + " → " + cur + "). Движение верное.";
    if (cur === prev) return "Уровень тревоги держится на прежнем уровне — стабильность тоже результат.";
    return "Опросник немного выше прошлого (" + prev + " → " + cur + "). Плохие дни бывают — врач это увидит.";
  }

  el("btn-checkin").addEventListener("click", function () {
    var payload = buildPayload();
    pushLog({ ts: new Date().toISOString(), dose: draft.dose, mood: draft.mood,
      gad7: payload.gad7_total || null, phq9: payload.phq9_total || null, cssrs: payload.cssrs });
    sendCheckin(payload).then(function (r) {
      resetDraft();
      showFeedback(r.resp, payload);
      renderHistory();
    }).catch(function (err) {
      var box = el("checkin-error"); if (box) { box.textContent = err.message; box.hidden = false; }
    });
  });

  /* ================= полный опросник (survey.js рисует вопросы) ============ */
  var surveyState = null;

  el("btn-full-survey").addEventListener("click", startSurveyChain);

  function startSurveyChain() {
    surveyState = { chain: ["GAD7", "PHQ9", "CSSRS"], idx: 0, q: 0, answers: {}, results: {} };
    showView("survey");
    renderSurvey();
  }

  function currentSurvey() { return SURVEYS[surveyState.chain[surveyState.idx]]; }
  function visibleItems(s) {
    return s.items.filter(function (it) { return !it.onlyIf || it.onlyIf(surveyState.answers); });
  }

  function renderSurvey() {
    var s = currentSurvey();
    var items = visibleItems(s);
    if (surveyState.q >= items.length) { finishSurvey(); return; }
    var it = items[surveyState.q];
    var done = 0, totalLeft = 0;
    surveyState.chain.forEach(function (id, i) {
      var sv = SURVEYS[id];
      var vis = (i < surveyState.idx) ? sv.items.length :
        (i === surveyState.idx ? visibleItems(sv).length : 0);
      if (i < surveyState.idx) done += vis;
      totalLeft += vis;
    });
    el("survey-count").textContent = (done + surveyState.q + 1) + "/" + totalLeft;
    el("survey-bar").style.width = Math.round(100 * (done + surveyState.q) / Math.max(1, totalLeft)) + "%";
    el("survey-lead").textContent = surveyState.q === 0 ? (s.lead + " · " + s.intro) : s.lead;
    el("survey-question").textContent = "За последние 2 недели " + it.q + "?";
    el("survey-foot").textContent = it.foot || "";
    var box = el("survey-choices");
    box.innerHTML = "";
    var choices = it.choices || (function () {
      return ["Совсем нет", "Несколько дней", "Больше половины дней", "Почти каждый день"]
        .map(function (label, i) { return { v: i, label: label }; });
    })();
    choices.forEach(function (c) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "survey-choice"; b.textContent = c.label;
      b.addEventListener("click", function () {
        collectAnswer(s, it, c.v);
        surveyState.answers[it.flag || ("q" + surveyState.idx + "_" + surveyState.q)] = c.v;
        surveyState.q += 1;
        renderSurvey();
      });
      box.appendChild(b);
    });
    window.scrollTo(0, 0);
  }

  function collectAnswer(s, it, v) {
    var R = surveyState.results;
    if (s.id === "GAD7") {
      R.gad7 = (R.gad7 || 0) + v;
    } else if (s.id === "PHQ9") {
      R.phq9 = (R.phq9 || 0) + v;
      if (it.flag === "item9") R.item9 = v;
    } else if (s.id === "CSSRS") {
      R.cssrs = Math.max(R.cssrs || 0, v);
    }
  }

  function finishSurvey() {
    surveyState.idx += 1; surveyState.q = 0;
    if (surveyState.idx >= surveyState.chain.length) {
      var R = surveyState.results;
      draft.phq9 = { total: R.phq9 || 0 };
      draft.gad7 = { total: R.gad7 || 0 };
      draft.item9 = (R.item9 !== undefined) ? R.item9 : null;
      draft.cssrs = R.cssrs || 0;
      showView("home");
      el("card-today").hidden = false;
      el("card-feedback").hidden = true;
      el("step-mood").hidden = true;
      el("step-send").hidden = false;
      el("send-summary").textContent = "Опросники пройдены: GAD-7 = " + (R.gad7 || 0) +
        ", PHQ-9 = " + (R.phq9 || 0) + ". Проверьте шаг 3 и отправьте.";
      return;
    }
    renderSurvey();
  }
  el("btn-survey-exit").addEventListener("click", function () { showView("home"); });

  /* ================= «что-то беспокоит» (жалоба врачом) ================= */
  el("btn-send-side").addEventListener("click", function () {
    var txt = el("side-effect-text").value.trim();
    if (txt.length < 3) { el("se-error").textContent = "Опишите чуть подробнее (минимум 3 символа)"; return; }
    el("se-error").textContent = "";
    var payload = buildPayload();
    payload.side_note = txt.slice(0, 500);
    el("btn-send-side").disabled = true;
    sendCheckin(payload).then(function () {
      el("side-effect-text").value = "";
      el("se-result").innerHTML = "<div class='fb-insight'>✓ Ушло врачу. Если это срочно — позвоните ему (номер — в поликлинике).</div>";
      el("btn-send-side").disabled = false;
    }).catch(function (e) {
      el("se-error").textContent = e.message;
      el("btn-send-side").disabled = false;
    });
  });

  /* ================= прогресс ================= */
  function renderProgress() {
    var log = loadLog();
    var pts = log.filter(function (x) { return x.gad7; }).slice(0, 12).reverse();
    drawChart(pts.map(function (x) { return x.gad7; }));
    el("gad7-trend").textContent = pts.length >= 2
      ? ("Последний: " + pts[pts.length - 1].gad7 + " из 21 • предыдущий: " + pts[pts.length - 2].gad7 + ".")
      : "Пока мало данных — пройдите полный опросник дважды, чтобы увидеть тренд.";
    var strip = el("mood-strip"); strip.innerHTML = "";
    loadMoods().slice(-14).forEach(function (m) {
      var d = document.createElement("div");
      d.className = "mood-day";
      d.title = m.d;
      d.textContent = ["", "😔", "😕", "😐", "🙂", "😊"][m.m] || "·";
      strip.appendChild(d);
    });
    renderHistory();
  }

  function drawChart(vals) {
    var cv = el("chart-gad7"); if (!cv) return;
    var W = cv.offsetWidth || 320, H = 150;
    cv.width = W * 2; cv.height = H * 2;
    var ctx = cv.getContext("2d");
    ctx.scale(2, 2);
    var pad = 18;
    ctx.clearRect(0, 0, W, H);
    if (vals.length < 2) {
      ctx.fillStyle = "#9ab0b0"; ctx.font = "12px system-ui";
      ctx.fillText("Нужно минимум 2 опросника для графика", pad, H / 2);
      return;
    }
    var max = 21;
    function X(i) { return pad + (W - pad * 2) * i / (vals.length - 1); }
    function Y(v) { return pad + (H - pad * 2) * (1 - v / max); }
    ctx.strokeStyle = "#e2ecec";
    [0, 5, 10, 15, 21].forEach(function (v) {
      ctx.beginPath(); ctx.moveTo(pad, Y(v)); ctx.lineTo(W - pad, Y(v)); ctx.stroke();
      ctx.fillStyle = "#9ab0b0"; ctx.font = "9px system-ui"; ctx.fillText(String(v), 2, Y(v) + 3);
    });
    ctx.strokeStyle = "#007d79"; ctx.lineWidth = 2.5; ctx.beginPath();
    vals.forEach(function (v, i) { i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); });
    ctx.stroke();
    ctx.fillStyle = "#007d79";
    vals.forEach(function (v, i) { ctx.beginPath(); ctx.arc(X(i), Y(v), 3.4, 0, 7); ctx.fill(); });
  }

  function renderHistory() {
    var list = el("history-list"); if (!list) return;
    var log = loadLog().slice(0, 8);
    list.innerHTML = log.length ? log.map(function (x) {
      var d = new Date(x.ts);
      var bits = [d.toLocaleDateString("ru-RU") + " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })];
      if (x.mood) bits.push(["", "😔", "😕", "😐", "🙂", "😊"][x.mood]);
      if (x.gad7) bits.push("GAD-7: " + x.gad7);
      bits.push(x.dose === "no" ? "доза пропущена" : "доза ✓");
      return "<li>" + bits.join(" • ") + "</li>";
    }).join("") : "<li class='dim'>Пока пусто — отправьте первый чек-ин.</li>";
    var q = loadQueue();
    var note = el("offline-queue-note");
    note.style.display = q.length ? "" : "none";
    note.textContent = q.length ? ("В офлайн-очереди: " + q.length + " чек-ин(ов) — уйдут при появлении сети.") : "";
  }

  /* ================= вход/выход ================= */
  function enterDiary() {
    try { showView("home"); } catch (e) { alert("A:"+e.message+" | "+e.stack); }
    try { renderHistory(); } catch (e) { alert("B:"+e.message+" | "+e.stack); }
    try { flushQueue(); } catch (e) { alert("C:"+e.message+" | "+e.stack); }
  }

  el("btn-bind").addEventListener("click", function () {
    bind(el("bind-token").value.trim());
  });
  el("bind-token").addEventListener("keydown", function (e) {
    if (e.key === "Enter") bind(el("bind-token").value.trim());
  });
  el("btn-care-ok").addEventListener("click", function () {
    el("card-feedback").hidden = true;
    el("card-today").hidden = false;
  });
  var btnLogout = el("btn-logout");
  if (btnLogout) btnLogout.addEventListener("click", function () { STORE.clear(); location.reload(); });

  /* ================= init ================= */
  (function init() {
    var tok = QS.get("token");
    if (tok) el("bind-token").value = tok;
    if (STORE.session) { enterDiary(); } else { showView("bind"); }
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(function () { /* офлайн не критичен */ });
    }
    window.addEventListener("online", flushQueue);
  })();
})();
