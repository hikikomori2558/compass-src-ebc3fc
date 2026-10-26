/*
 * frontend_patient/app.js — PWA-дневник пациента COMPASS (Duolingo-механика).
 *
 * Механика (снята с живого Duolingo, адаптирована «без чувства вины»):
 *   - один вопрос на экран; варианты — карточки с жёсткой нижней тенью;
 *     выбор подсвечивает карточку и активирует нижнюю кнопку «Продолжить»;
 *   - прогресс-бар урока сверху; выход ✕ в любой момент («прогресс сохранится»);
 *   - серия дней подряд (streak) — без давления: пропуски не «сжигают» серию
 *     в формулировках, никаких стыдящих текстов;
 *   - НЕМЕДЛЕННЫЙ фидбек после отправки; при «тяжёлом» ответе — поддерживающий
 *     экран с кризисным телефоном, без красноты и без «сигналов тревоги».
 *
 * Клинический контур не менялся: QR → bind → чек-ин (идемпотентный event_uuid,
 * офлайн-очередь) → врачу уходит пустой WS-пинг, клиника остаётся в контуре МО.
 */
(function () {
  "use strict";

  var QS = new URLSearchParams(window.location.search);
  var SAME_ORIGIN = (location.origin && location.origin.indexOf("http") === 0) ? location.origin : "http://127.0.0.1:8000";
  var CFG = { apiBase: (QS.get("api") || localStorage.getItem("compass_patient_api") || SAME_ORIGIN).replace(/\/+$/, "") };
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
  var DIARY_KEY = "compass_patient_diary";
  var STREAK_KEY = "compass_patient_streak";

  function el(id) { return document.getElementById(id); }
  function uuid() {
    return (crypto.randomUUID) ? crypto.randomUUID() :
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
  }
  function today() { return new Date().toISOString().slice(0, 10); }
  function dayDiff(d1, d2) { return Math.round((new Date(d2) - new Date(d1)) / 864e5); }

  function api(path, opts, token, flags) {
    opts = opts || {};
    var noReauth = !!(flags && flags.queue);
    return fetch(CFG.apiBase + path, Object.assign({}, opts, {
      headers: Object.assign({ "Content-Type": "application/json" },
        token ? { "Authorization": "Bearer " + token } : {}, opts.headers || {}),
    })).then(function (r) {
      if (r.status === 401) {
        if (noReauth) throw new Error("401-queue");
        STORE.clear(); showView("bind");
        throw new Error("Сессия истекла — привяжитесь заново");
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* ================= очередь / журнал / серия ================= */
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
      api("/api/v1/patient/checkin", { method: "POST", body: JSON.stringify(item.payload) }, t.access_token, { queue: true })
        .then(next)
        .catch(function () { rest.push(item); rest = rest.concat(q); saveQueue(rest); renderHistory(); });
    })();
  }
  function loadLog() { try { return JSON.parse(localStorage.getItem(LOG_KEY) || "[]"); } catch (e) { return []; } }
  function pushLog(entry) { var l = loadLog(); l.unshift(entry); localStorage.setItem(LOG_KEY, JSON.stringify(l.slice(0, 90))); }
  function loadMoods() { try { return JSON.parse(localStorage.getItem(MOOD_KEY) || "[]"); } catch (e) { return []; } }
  function pushMood(m) {
    var l = loadMoods().filter(function (x) { return x.d !== today(); });
    l.push({ d: today(), m: m }); localStorage.setItem(MOOD_KEY, JSON.stringify(l.slice(-30)));
  }
  /* серия: дней подряд с хотя бы одним чек-ином; разрыв не «сжигает» её в тексте —
     просто показываем текущую непрерывную серию, без упрёков. */
  function calcStreak() {
    var days = loadLog().map(function (x) { return x.ts.slice(0, 10); });
    days = Array.from(new Set(days)).sort().reverse();
    if (!days.length) return 0;
    var streak = 1;
    for (var i = 1; i < days.length; i++) {
      if (dayDiff(days[i], days[i - 1]) === 1) streak += 1; else break;
    }
    return streak;
  }
  function applyStreak(streak) {
    [["streak", "streak-n"], ["streak2", "streak2-n"], ["survey-streak", "survey-streak-n"], ["ob-streak", "ob-streak-n"]]
      .forEach(function (pair) {
        var wrap = el(pair[0]), n = el(pair[1]);
        if (!wrap) return;
        wrap.hidden = streak < 1;
        if (n) n.textContent = String(streak);
      });
  }

  function renderStreaks() {
    // Локальный расчёт — мгновенный отклик и офлайн; серверный — источник истины
    applyStreak(calcStreak());
    var t = STORE.session;
    if (t && t.access_token) {
      api("/api/v1/patient/engagement", {}, t.access_token)
        .then(function (d) {
          if (d && typeof d.streak_days === "number") {
            applyStreak(d.streak_days);
            localStorage.setItem(STREAK_KEY, String(d.streak_days));
            renderMilestone(d);
          }
        }).catch(function () { /* офлайн: остаётся локальный */ });
    }
  }

  /* Веха недели по коридорам РКИ — карточка без давления (Этап 6.3) */
  function renderMilestone(eng) {
    var box = el("milestone-card");
    if (!box) return;
    var day = therapyDayEstimate();
    var week = Math.min(8, Math.max(1, Math.ceil(day / 7)));
    var text = {
      1: "Неделя 1 — период адаптации: сонливость и головокружение обычно уходят к 5–7 дню. Приём — за 30 минут до еды.",
      2: "Неделя 2 — период, когда эффект начинает проявляться. Сон выравнивается раньше настроения.",
      3: "Неделя 3 — поворот по РКИ: динамика тревоги становится заметной (Ivashchenko 2021).",
      4: "Неделя 4 — контрольная точка КР МЗ РФ: врач оценивает ответ (редукция ≥25%).",
      5: "Неделя 5+ — закрепление эффекта. Серия дней — ваш вклад в результат.",
      6: "Неделя 6 — курс продолжается по плану врача.",
      7: "Неделя 7 — эффект накапливается (РКИ III: ответ растёт к 32 нед).",
      8: "Неделя 8 — контрольная точка КР МЗ РФ: ремиссия GAD-7 <5 — цель.",
    }[week];
    var extra = (eng.streak_days >= 3) ? " Серия: " + eng.streak_days + " дн. без пропусков — держится стабильно." : "";
    box.hidden = false;
    box.innerHTML = "<b>" + text + "</b>" + extra +
      (eng.best_streak > eng.streak_days ? " <span class='sb-dim'>Рекорд: " + eng.best_streak + " дн.</span>" : "");
  }

  function therapyDayEstimate() {
    var l = loadLog();
    return Math.max(1, l.length);   // дней с чек-инами
  }

  /* Связь с врачом (задача 6 кейса): отправка + тред */
  function renderMsgThread(rows) {
    var box = el("msg-thread");
    if (!box) return;
    box.classList.add("chat");
    box.innerHTML = (rows || []).reverse().map(function (m) {
      var pat = m.sender_role === "PATIENT";
      return "<div class='chat-msg " + (pat ? "chat-msg--pat" : "chat-msg--doc") + "'>" +
        (pat ? "" : "<b>Врач:</b> ") + String(m.body).replace(/[<>]/g, "") +
        "<span class='when'>" + new Date(m.created_at).toLocaleString("ru-RU") + "</span></div>";
    }).join("") || "";
  }
  function refreshMsgThread() {
    var t = STORE.session;
    if (!t || !t.access_token) return;
    api("/api/v1/patient/messages", {}, t.access_token)
      .then(function (d) { renderMsgThread(d && d.messages); }).catch(function () {});
  }
  var msgBtn = el("btn-msg-send");
  if (msgBtn) {
    msgBtn.addEventListener("click", function () {
      var ta = el("msg-body");
      var text = (ta.value || "").trim();
      if (text.length < 2) return;
      msgBtn.disabled = true; msgBtn.textContent = "Отправляю…";
      var t = STORE.session;
      api("/api/v1/patient/messages", { method: "POST", body: JSON.stringify({ body: text }) },
          t.access_token)
        .then(function () { ta.value = ""; refreshMsgThread();
          msgBtn.textContent = "Отправлено ✓"; setTimeout(function () {
            msgBtn.textContent = "Отправить врачу"; msgBtn.disabled = false; }, 2000); })
        .catch(function (e) {
          msgBtn.textContent = "Ошибка: " + e.message; msgBtn.disabled = false; });
    });
    refreshMsgThread();
  }

  /* ================= навигация ================= */
  function showView(name) {
    ["bind", "onboard", "home", "survey", "progress", "info"].forEach(function (v) {
      var node = el("view-" + v);
      if (node) node.style.display = (v === name) ? "" : "none";
    });
    document.querySelectorAll(".duo-tab").forEach(function (c) {
      c.classList.toggle("duo-tab--active", c.dataset.go === name);
    });
    if (name === "progress") renderProgress();
    if (name === "home") renderStreaks();
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
        localStorage.setItem("compass_onboard_done", localStorage.getItem("compass_onboard_done") || "0");
        if (localStorage.getItem("compass_onboard_done") === "1") enterHome();
        else showView("onboard");
      })
      .catch(function (err) {
        el("bind-error").textContent = "Не удалось привязаться: " + err.message +
          ". Код одноразовый — попросите врача показать QR заново.";
      })
      .finally(function () { btn.disabled = false; btn.textContent = "Привязаться"; });
  }

  /* ================= onboarding ================= */
  el("btn-ob-start").addEventListener("click", function () {
    localStorage.setItem("compass_onboard_done", "1");
    enterHome();
  });
  el("btn-ob-done").addEventListener("click", function () {
    localStorage.setItem("compass_onboard_done", "1");
    enterHome();
  });

  /* ================= чек-ин: 3 тапа ================= */
  var draft = { dose: null, mood: null, phq9: null, gad7: null, cssrs: null, item9: null,
    diary: null };

  function resetDraft() {
    draft = { dose: null, mood: null, phq9: null, gad7: null, cssrs: null, item9: null, diary: null };
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
        ". Опросник недели и подробный дневник — кнопками ниже: врачу важна динамика.";
    }
  }

  document.querySelectorAll(".choice").forEach(function (c) {
    c.addEventListener("click", function () {
      document.querySelectorAll(".choice").forEach(function (x) { x.classList.remove("choice--on"); });
      c.classList.add("choice--on");
      draft.dose = c.dataset.dose;
      if (draft.dose === "yes") {
        var it = document.createElement("input");
        it.type = "time"; it.className = "duo-input"; it.value = "08:00";
        it.id = "intake-time"; it.setAttribute("aria-label", "Время приёма");
        var row = c.parentElement.parentElement;
        var existing = document.getElementById("intake-time");
        if (!existing) { var lbl = document.createElement("div"); lbl.className = "qtitle"; lbl.style.fontSize = "14px"; lbl.textContent = "Во сколько приняли?"; row.appendChild(lbl); row.appendChild(it); }
        else { existing.value = "08:00"; }
      }
      goStep(2);
    });
  });

  /* настроение: 5 состояний, SVG-иконки (без эмодзи) */
  var MOOD_FACES = [
    { m: 1, label: "тяжело", mouth: "M8 16c1.5-2 6.5-2 8 0" },
    { m: 2, label: "так себе", mouth: "M8.5 15.5c1.2-1 5.8-1 7 0" },
    { m: 3, label: "нормально", mouth: "M8 15h8" },
    { m: 4, label: "хорошо", mouth: "M8.5 14.5c1.2 1 5.8 1 7 0" },
    { m: 5, label: "отлично", mouth: "M8 14c1.5 2 6.5 2 8 0" },
  ];
  (function buildMoodRow() {
    var row = el("mood-row");
    MOOD_FACES.forEach(function (f) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "mood"; b.dataset.mood = f.m;
      b.innerHTML =
        '<svg viewBox="0 0 24 24" width="30" height="30">' +
        '<circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.8"/>' +
        '<circle cx="8.5" cy="10" r="1.3" fill="currentColor"/><circle cx="15.5" cy="10" r="1.3" fill="currentColor"/>' +
        '<path d="' + f.mouth + '" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>' +
        "<span>" + f.label + "</span>";
      b.addEventListener("click", function () {
        document.querySelectorAll(".mood").forEach(function (x) { x.classList.remove("mood--on"); });
        b.classList.add("mood--on");
        draft.mood = f.m;
        pushMood(f.m);
        goStep(3);
      });
      row.appendChild(b);
    });
  })();

  /* ================= отправка + фидбек ================= */
  function buildPayload() {
    return {
      event_uuid: uuid(),
      phq9_total: (draft.phq9 && draft.phq9.total) || 0,
      phq9_item9: (draft.item9 !== null && draft.item9 !== undefined) ? draft.item9 : null,
      gad7_total: (draft.gad7 && draft.gad7.total) || 0,
      hads_answers: draft.hads_answers || null,
      cssrs: (draft.cssrs !== null && draft.cssrs !== undefined) ? draft.cssrs : null,
      took_medication: draft.dose !== "no",
      intake_time: (draft.dose === "yes" && el("intake-time")) ? el("intake-time").value || null : null,
      diary: draft.diary || null,
    };
  }

  function sendCheckin(payload) {
    var t = STORE.session;
    if (!t || !t.access_token) { showView("bind"); return Promise.reject(new Error("Сессия истекла")); }
    var btn = el("btn-checkin"); btn.disabled = true; btn.textContent = "Отправляю…";
    return api("/api/v1/patient/checkin", { method: "POST", body: JSON.stringify(payload) }, t.access_token)
      .then(function (resp) { return { resp: resp, offline: false }; })
      .catch(function (err) {
        var m = String(err.message);
        if (m === "401-queue" || m.indexOf("HTTP") === 0 || m.indexOf("Failed to fetch") >= 0 || m.indexOf("NetworkError") >= 0) {
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
    if (cur === prev) return "Уровень тревоги держится ровно — стабильность тоже результат.";
    return "Опросник немного выше прошлого (" + prev + " → " + cur + "). Плохие дни бывают — врач это увидит.";
  }

  el("btn-checkin").addEventListener("click", function () {
    var payload = buildPayload();
    pushLog({ ts: new Date().toISOString(), dose: draft.dose, mood: draft.mood,
      gad7: payload.gad7_total || null, phq9: payload.phq9_total || null, cssrs: payload.cssrs,
      diary: draft.diary || null });
    sendCheckin(payload).then(function (r) {
      resetDraft();
      showFeedback(r.resp, payload);
      renderStreaks();
      renderHistory();
    }).catch(function (err) {
      var box = el("checkin-error"); if (box) { box.textContent = err.message; box.hidden = false; }
    });
  });

  /* ================= шкалы МЗ РФ с сервера (item-level, hot-reload) =================
   * Опросники тянутся GET /scales/{code}: формулировки и tips правятся на сервере
   * без релиза PWA (Метрика №10). Офлайн — встроенный снимок ниже. */
  var FALLBACK_GAD7 = {
    id: "GAD7", lead: "Неделя · тревога",
    intro: "7 коротких вопросов о последних двух неделях.",
    items: [
      "Ощущение нервозности, тревожности или напряжения",
      "Невозможность остановить волнение или контролировать беспокойство",
      "Чрезмерное беспокойство о разных вещах",
      "Трудности с расслаблением",
      "Такое беспокойство, что трудно усидеть на месте",
      "Лёгкая раздражительность или вспыльчивость",
      "Страх, что случится что-то ужасное",
    ],
  };
  var FALLBACK_HADS = {
    id: "HADS", lead: "Неделя · настроение и тревога",
    intro: "14 утверждений за последнюю неделю — как в бланке поликлиники.",
    items: [
      "Я испытываю напряженность или взвинченность",
      "Мне по-прежнему доставляют удовольствие вещи, которые радовали меня раньше",
      "Я испытываю страх, будто что-то ужасное может вот-вот случиться",
      "Я способен рассмеяться и увидеть в происходящем смешную сторону",
      "Беспокоящие мысли крутятся у меня в голове",
      "Я чувствую себя бодрым(-ой)",
      "Я могу спокойно сесть и легко расслабиться",
      "У меня такое ощущение, словно я двигаюсь заторможенно",
      "Я испытываю внутреннее напряжение или дрожь",
      "Я не могу усидеть на месте, мне постоянно нужно двигаться",
      "Я потерял(-а) интерес к своей внешности",
      "Я жду с удовольствием того, что должно произойти",
      "Я испытываю внезапное чувство паники",
      "Я могу получить удовольствие от хорошей книги, радио- или телепередачи",
    ],
    itemOptions: [
      ["Совсем не испытываю", "Время от времени, иногда", "Часто", "Большую часть времени"],
      ["Определённо да", "Пожалуй, да", "В очень малой степени", "Совсем нет"],
      ["Совсем не испытываю", "Иногда, и это меня не беспокоит", "Да, но страх не сильный", "Определённо да, страх очень сильный"],
      ["Определённо да", "Пожалуй, да", "В очень малой степени", "Совсем не способен"],
      ["Только изредка", "Время от времени и не так часто", "Большую часть времени", "Постоянно"],
      ["Практически всё время", "Иногда", "Очень редко", "Совсем не чувствую"],
      ["Определённо да", "Пожалуй, да", "Лишь изредка", "Совсем не могу"],
      ["Совсем нет", "Иногда", "Часто", "Практически всё время"],
      ["Совсем не испытываю", "Иногда", "Часто", "Очень часто"],
      ["Совсем нет", "Не столь сильно", "Довольно сильно", "Очень определённо"],
      ["Нет, интерес не изменился", "Несколько меньше, чем раньше", "Значительно меньше", "Совсем не интересует"],
      ["Как обычно", "Меньше, чем раньше", "Значительно меньше", "Совсем не жду"],
      ["Совсем не бывает", "Не так уж часто", "Довольно часто", "Очень часто"],
      ["Часто", "Иногда", "Редко", "Очень редко или никогда"],
    ],
  };

  function surveyFromScale(def, fallback, leadKey) {
    var items = (def && def.items ? def.items : fallback.items).map(function (q, i) {
      var it = { q: (def && def.items ? String(q) : q) };
      var opts = (def && def.item_options) ? def.item_options[i]
        : (fallback.itemOptions ? fallback.itemOptions[i] : null);
      if (opts) {
        it.choices = opts.map(function (o, j) {
          return { v: (typeof o === "object" ? o.v : j), label: (typeof o === "object" ? o.label : o) };
        });
      } else {
        it.freq = true;
      }
      var tip = def && def.tips ? def.tips[i] : null;
      if (tip) it.tip = tip;
      return it;
    });
    return { id: fallback.id, lead: fallback.lead, intro: fallback.intro, items: items };
  }

  function startFullSurvey() {
    var defs = { gad: null, hads: null };
    var done = 0;
    var launch = function () {
      var gad = surveyFromScale(defs.gad, FALLBACK_GAD7);
      var hads = surveyFromScale(defs.hads, FALLBACK_HADS);
      SURVEYS.GAD7 = gad;
      SURVEYS.HADS = hads;
      run = { chain: ["GAD7", "HADS", "SAFETY"], idx: 0, q: 0, answers: {}, results: {}, mode: "survey" };
      showView("survey");
      renderStep();
    };
    var got = function () { done += 1; if (done >= 2) launch(); };
    var tk = (STORE.session && STORE.session.access_token) || null;
    api("/api/v1/compass-bio/scales/GAD-7", {}, tk)
      .then(function (d) { defs.gad = d; got(); }).catch(got);
    api("/api/v1/compass-bio/scales/HADS", {}, tk)
      .then(function (d) { defs.hads = d; got(); }).catch(got);
    // защита от зависшего запроса: через 4 с стартуем на том, что есть
    setTimeout(function () { if (done < 2) { done = 2; launch(); } }, 4000);
  }

  /* ================= движок шагов (опросник / дневник) ================= */
  var run = null; // {chain, idx, q, answers, results, mode}

  el("btn-full-survey").addEventListener("click", function () {
    startFullSurvey();
    showView("survey");
    renderStep();
  });
  var openDiary = function () {
    run = { chain: ["DIARY"], idx: 0, q: 0, answers: {}, results: {}, mode: "diary" };
    showView("survey");
    renderStep();
  };
  el("btn-diary-full").addEventListener("click", openDiary);
  el("btn-show-onboard").addEventListener("click", function () { showView("onboard"); });
  el("btn-diary-quick").addEventListener("click", openDiary);

  el("btn-survey-exit").addEventListener("click", function () {
    run = null;
    showView("home"); // без упрёков: прогресс частично сохранён
  });

  function currentSurvey() { return SURVEYS[run.chain[run.idx]]; }
  function visibleItems(s) {
    return s.items.filter(function (it) { return !it.onlyIf || it.onlyIf(run.answers); });
  }

  var picked = null;      // текущий выбор (для многошагового «Продолжить»)
  var multiPicked = [];   // для multi-чипов

  function renderStep() {
    if (!run) return; // защита: цепочка уже завершена (клик по мёртвому DOM)
    var s = currentSurvey();
    var items = visibleItems(s);
    if (run.q >= items.length) { finishChain(); return; }
    var it = items[run.q];

    var done = 0, totalLeft = 0;
    run.chain.forEach(function (id, i) {
      var sv = SURVEYS[id];
      var vis = (i < run.idx) ? sv.items.length :
        (i === run.idx ? visibleItems(sv).length : 0);
      if (i < run.idx) done += vis;
      totalLeft += vis;
    });
    el("survey-count-text") && 0;
    el("survey-bar").style.width = Math.round(100 * (done + run.q) / Math.max(1, totalLeft)) + "%";
    el("survey-lead").textContent = (run.q === 0 ? s.lead + " · " + s.intro : s.lead);
    el("survey-question").textContent = it.q;
    var footText = it.tip || it.foot || "";
    el("survey-foot").hidden = !footText;
    el("survey-foot").textContent = footText;

    var box = el("survey-choices");
    box.innerHTML = "";
    picked = null; multiPicked = [];
    var nextBtn = el("btn-survey-next");
    nextBtn.disabled = true;
    nextBtn.textContent = it.cta || "Продолжить";

    if (it.input) {
      // свободный ввод (заметка дневника)
      var ta = document.createElement("textarea");
      ta.className = "duo-input duo-input--area"; ta.rows = 4;
      ta.placeholder = "Пара слов своими словами (необязательно)";
      ta.addEventListener("input", function () { nextBtn.disabled = false; });
      box.appendChild(ta);
      nextBtn.onclick = function () {
        run.answers[it.flag] = ta.value.trim();
        collectAnswer(s, it, ta.value.trim());
        run.q += 1; renderStep();
      };
      return;
    }

    (it.choices || (function () {
      return FREQ.map(function (label, i) { return { v: i, label: label }; });
    })()).forEach(function (c) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "survey-choice"; b.textContent = c.label;
      b.addEventListener("click", function () {
        if (it.tips && it.tips[c.v]) {
          var foot = el("survey-foot");
          foot.hidden = false;
          foot.textContent = it.tips[c.v];
          foot.className = "survey-foot survey-foot--tip";
        }
        if (it.multi) {
          if (c.v === "Ничего из этого") multiPicked = [c];
          else {
            multiPicked = multiPicked.filter(function (x) { return x.v !== "Ничего из этого"; });
            if (b.classList.contains("choice--on")) b.classList.remove("choice--on");
            else b.classList.add("choice--on");
            if (!multiPicked.some(function (x) { return x.v === c.v; })) multiPicked.push(c);
          }
          Array.from(box.children).forEach(function (elBtn) {
            var lbl = elBtn.textContent;
            var on = multiPicked.some(function (x) { return x.v === lbl; });
            elBtn.classList.toggle("choice--on", on);
          });
          nextBtn.disabled = multiPicked.length === 0;
        } else {
          Array.from(box.children).forEach(function (x) { x.classList.remove("choice--on"); });
          b.classList.add("choice--on");
          picked = c;
          nextBtn.disabled = false;
        }
      });
      box.appendChild(b);
    });

    if (it.skip) {
      var sk = document.createElement("button");
      sk.type = "button"; sk.className = "survey-choice survey-choice--skip";
      sk.textContent = "Пропустить вопрос";
      sk.addEventListener("click", function () {
        run.answers[it.flag] = null;
        if (it.flag === "item9") run.results.item9 = null;
        run.q += 1;
        renderStep();
      });
      box.appendChild(sk);
    }

    nextBtn.onclick = function () {
      if (!run) return;
      if (it.multi) {
        run.answers[it.flag] = multiPicked.map(function (x) { return x.v; });
        collectAnswer(s, it, multiPicked.map(function (x) { return x.v; }).join(", "));
      } else {
        run.answers[it.flag] = picked.v;
        collectAnswer(s, it, picked.v);
      }
      run.q += 1;
      renderStep();
    };
  }

  function collectAnswer(s, it, v) {
    var R = run.results;
    if (s.id === "GAD7") {
      R.gad7 = (R.gad7 || 0) + (typeof v === "number" ? v : 0);
    } else if (s.id === "HADS") {
      R.hads = R.hads || [];
      R.hads.push(v);
    } else if (s.id === "PHQ9") {
      R.phq9 = (R.phq9 || 0) + (typeof v === "number" ? v : 0);
      if (it.flag === "item9") R.item9 = v;
    } else if (s.id === "SAFETY") {
      R.cssrs = Math.max(R.cssrs || 0, typeof v === "number" ? v : 0);
    } else if (s.id === "DIARY") {
      R.diary = R.diary || {};
      R.diary[it.flag] = v;
    }
  }

  function finishChain() {
    var R = run.results, mode = run.mode;
    run = null;
    showView("home");
    if (mode === "survey") {
      draft.phq9 = { total: 0 };
      draft.gad7 = { total: R.gad7 || 0 };
      draft.hads_answers = R.hads || null;
      draft.item9 = (R.item9 !== undefined) ? R.item9 : null;
      draft.cssrs = R.cssrs || 0;
      el("card-today").hidden = false;
      el("card-feedback").hidden = true;
      el("step-mood").hidden = true;
      el("step-send").hidden = false;
      el("send-summary").textContent = "Опросники пройдены: GAD-7 = " + (R.gad7 || 0) +
        (R.hads ? ", HADS = " + R.hads.reduce(function (a, b) { return a + b; }, 0) + " (14 пунктов)" : "") +
        ". Проверьте и отправьте.";
    } else {
      draft.diary = R.diary || {};
      el("card-today").hidden = false;
      el("card-feedback").hidden = true;
      el("step-mood").hidden = true;
      el("step-send").hidden = false;
      var d = R.diary || {};
      el("send-summary").textContent = "Дневник сохранён: сон " + (d.sleep || "—") +
        ", силы " + (d.energy ? d.energy + "/4" : "—") +
        (d.symptoms ? ", отмечено: " + d.symptoms : "") + ". Отправьте вместе с чек-ином.";
    }
  }

  /* ================= «что-то беспокоит» ================= */
  el("btn-send-side").addEventListener("click", function () {
    var txt = el("side-effect-text").value.trim();
    if (txt.length < 3) { el("se-error").textContent = "Опишите чуть подробнее (минимум 3 символа)"; return; }
    el("se-error").textContent = "";
    var payload = buildPayload();
    payload.side_note = txt.slice(0, 500);
    el("btn-send-side").disabled = true;
    sendCheckin(payload).then(function () {
      el("side-effect-text").value = "";
      el("se-result").innerHTML = "<div class='fb-insight'>Ушло врачу. Если это срочно — позвоните ему (номер — в поликлинике).</div>";
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
      : "Пока мало данных — пройдите опросник дважды, чтобы увидеть тренд.";
    var strip = el("mood-strip"); strip.innerHTML = "";
    loadMoods().slice(-14).forEach(function (m) {
      var d = document.createElement("div");
      d.className = "mood-day";
      d.title = m.d;
      d.appendChild(moodFace(m.m, 20));
      strip.appendChild(d);
    });
    renderHistory();
  }

  function moodFace(m, size) {
    var f = MOOD_FACES[m - 1] || MOOD_FACES[2];
    var wrap = document.createElement("span");
    wrap.innerHTML = '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '">' +
      '<circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.8"/>' +
      '<circle cx="8.5" cy="10" r="1.3" fill="currentColor"/><circle cx="15.5" cy="10" r="1.3" fill="currentColor"/>' +
      '<path d="' + f.mouth + '" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
    return wrap.firstChild;
  }

  function drawChart(vals) {
    var cv = el("chart-gad7"); if (!cv) return;
    var W = cv.offsetWidth || 320, H = 150;
    cv.width = W * 2; cv.height = H * 2;
    var ctx = cv.getContext("2d");
    ctx.scale(2, 2);
    // Референс-линии КР МЗ РФ (Г2): ответ = редукция ≥50% от исходного; ремиссия <5.
    // Если исходный балл известен (первая точка), рисуем обе; ремиссия — всегда.
    var maxV = 21, base = vals && vals.length ? vals[0] : null;
    var yOf = function (v) { return H - 18 - (v / maxV) * (H - 30); };
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.font = "10px system-ui"; ctx.textAlign = "left";
    if (base && base > 0) {
      var resp = Math.max(0, base * 0.5), yr = yOf(resp);
      ctx.strokeStyle = "#d97706"; ctx.beginPath(); ctx.moveTo(28, yr); ctx.lineTo(W - 4, yr); ctx.stroke();
      ctx.fillStyle = "#d97706"; ctx.fillText("ответ (−50%)", 30, yr - 3);
    }
    var yrem = yOf(5);
    ctx.strokeStyle = "#059669"; ctx.beginPath(); ctx.moveTo(28, yrem); ctx.lineTo(W - 4, yrem); ctx.stroke();
    ctx.fillStyle = "#059669"; ctx.fillText("ремиссия <5", 30, yrem - 3);
    ctx.restore();
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
      if (x.mood) bits.push("настроение " + x.mood + "/5");
      if (x.gad7) bits.push("GAD-7: " + x.gad7);
      if (x.diary && x.diary.sleep) bits.push("сон: " + x.diary.sleep);
      bits.push(x.dose === "no" ? "доза пропущена" : "доза ✓");
      return "<li>" + bits.join(" • ") + "</li>";
    }).join("") : "<li class='dim'>Пока пусто — отправьте первый чек-ин.</li>";
    var q = loadQueue();
    var note = el("offline-queue-note");
    if (note) {
      note.style.display = q.length ? "" : "none";
      note.textContent = q.length ? ("В офлайн-очереди: " + q.length + " чек-ин(ов) — уйдут при появлении сети.") : "";
    }
  }

  /* ================= вход/выход ================= */
  function enterHome() {
    refreshMsgThread();
    showView("home");
    renderStreaks();
    renderHistory();
    flushQueue();
  }

  el("btn-bind").addEventListener("click", function () {
    bind(el("bind-token").value.trim());
  });
  el("bind-token").addEventListener("input", function () {
    el("btn-bind").disabled = el("bind-token").value.trim().length < 10;
  });
  // sync при программной установке (QR-переход, вставка через меню) — input не стреляет
  new MutationObserver(function () {
    el("btn-bind").disabled = el("bind-token").value.trim().length < 10;
  }).observe(el("bind-token"), { attributes: true, attributeFilter: ["value"] });
  el("bind-token").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !el("btn-bind").disabled) bind(el("bind-token").value.trim());
  });
  function closeFeedback() {
    el("card-feedback").hidden = true;
    el("card-today").hidden = false;
    resetDraft();      // форма чек-ина заново с шага 1 (раньше застревала на шаге 3)
    goStep(1);
  }
  el("btn-care-ok").addEventListener("click", closeFeedback);
  el("btn-care-ok2").addEventListener("click", closeFeedback);
  var btnLogout = el("btn-logout");
  if (btnLogout) btnLogout.addEventListener("click", function () { STORE.clear(); location.reload(); });

  /* ================= init ================= */
  (function init() {
    // Демо-вход от лица синтетического пациента (QR/кнопка из когорты врача):
    // токен тот же PATIENT-access, клейм demo -> баннер «синтетические данные»
    var demoTok = QS.get("demo");
    if (demoTok) {
      try {
        var pay = JSON.parse(atob(demoTok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
        if (pay.demo) document.documentElement.setAttribute("data-demo", "synthetic");
      } catch (e) { /* не разбирается — просто не показываем баннер */ }
      STORE.session = { access_token: demoTok };
      localStorage.setItem("compass_patient_session", JSON.stringify(STORE.session));
      // демо-вход жюри: онбординг уже пройден «пациентом» — сразу в его дом
      localStorage.setItem("compass_onboard_done", "1");
      history.replaceState(null, "", location.pathname);
    }
    var tok = QS.get("token");
    if (tok) {
      el("bind-token").value = tok;
      el("btn-bind").disabled = false; // QR-переход: кнопка активна сразу (валидацию делает сервер)
    }
    if (STORE.session) {
      if (localStorage.getItem("compass_onboard_done") === "1") enterHome();
      else showView("onboard");
    } else { showView("bind"); }
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(function () { });
    }
    window.addEventListener("online", flushQueue);
  })();
})();
