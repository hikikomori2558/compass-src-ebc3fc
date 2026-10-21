/*
 * frontend_patient/app.js — PWA-дневник пациента (контур Условия 2).
 *
 * Поток: QR от врача -> /api/v1/auth/bind (одноразовый токен, ИДС автоматически)
 *        -> пациентский JWT -> ежедневный чек-ин POST /api/v1/patient/checkin
 *        (идемпотентный event_uuid + офлайн-очередь; при критичных ответах врачу
 *        уходит ПУСТОЙ WS-пинг — клинический контент остаётся в контуре МО).
 *
 * Дизайн-принципы (из чек-листа кейса):
 *   - никаких советов/решений пациенту (Условие 3): только фиксация и передача врачу;
 *   - пропуск вопроса о суицидальном риске НЕ считается «нулём»: сервер честно
 *     помечает suicide_item_missing и PWA переспрашивает.
 */
(function () {
  "use strict";

  var QS = new URLSearchParams(window.location.search);
  var CFG = {
    apiBase: (QS.get("api") || localStorage.getItem("compass_patient_api") || "http://127.0.0.1:8000").replace(/\/+$/, "")
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

  function el(id) { return document.getElementById(id); }
  function uuid() {
    return (crypto.randomUUID) ? crypto.randomUUID() :
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
  }

  // ------------------------------------------------------------------ API
  function api(path, opts, token) {
    return fetch(CFG.apiBase + path, Object.assign({}, opts, {
      headers: Object.assign(
        { "Content-Type": "application/json" },
        token ? { "Authorization": "Bearer " + token } : {},
        (opts && opts.headers) || {}
      )
    }));
  }

  function withRefresh(fn) {
    var s = STORE.session;
    if (!s) return Promise.reject(new Error("нет сессии"));
    return fn(s.access).then(function (r) {
      if (r.status !== 401) return r;
      return api("/api/v1/auth/refresh", {
        method: "POST", body: JSON.stringify({ refresh_token: s.refresh })
      }).then(function (rr) {
        if (!rr.ok) { STORE.clear(); throw new Error("сессия истекла, привяжитесь по QR снова"); }
        return rr.json().then(function (d) {
          STORE.session = { access: d.access_token, refresh: d.refresh_token, sub: d.principal_sub };
          return fn(d.access_token);
        });
      });
    });
  }

  // ------------------------------------------------------------------ офлайн-очередь
  function queueLoad() {
    try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch (e) { return []; }
  }
  function queueSave(q) { localStorage.setItem(QUEUE_KEY, JSON.stringify(q)); }

  function flushQueue() {
    var q = queueLoad();
    if (!q.length || !STORE.session) { updateQueueNote(); return; }
    var item = q[0];
    withRefresh(function (t) {
      return api("/api/v1/patient/checkin", { method: "POST", body: JSON.stringify(item.payload) }, t);
    }).then(function (r) {
      if (r.status === 201 || r.status === 200) {
        q.shift(); queueSave(q);
        historyAdd(item.payload, true);
        flushQueue();
      }
    }).catch(function () { /* сети нет — останется в очереди */ }).then(updateQueueNote);
  }

  function updateQueueNote() {
    var n = el("offline-queue-note");
    var q = queueLoad();
    if (q.length) {
      n.style.display = "block";
      n.textContent = "Не отправлено (нет связи): " + q.length +
        " чек-ин(ов). Отправим автоматически при восстановлении соединения.";
    } else {
      n.style.display = "none";
    }
  }

  // ------------------------------------------------------------------ история (локальная)
  function historyLoad() {
    try { return JSON.parse(localStorage.getItem("compass_patient_history") || "[]"); } catch (e) { return []; }
  }
  function historyAdd(payload, delivered) {
    var h = historyLoad();
    h.unshift({
      ts: new Date().toISOString(),
      gad7: payload.gad7_total,
      dose: payload.took_medication,
      delivered: !!delivered
    });
    localStorage.setItem("compass_patient_history", JSON.stringify(h.slice(0, 60)));
    renderHistory();
  }
  function renderHistory() {
    var list = el("history-list");
    list.innerHTML = "";
    historyLoad().forEach(function (h) {
      var li = document.createElement("li");
      li.className = h.delivered ? "hist-item" : "hist-item hist-item--pending";
      var d = new Date(h.ts);
      li.textContent = d.toLocaleDateString("ru-RU") + " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) +
        " • GAD-7: " + h.gad7 + " • доза: " + (h.dose ? "принята" : "пропущена") +
        (h.delivered ? "" : " • ждёт отправки");
      list.appendChild(li);
    });
    if (!list.children.length) {
      var li0 = document.createElement("li");
      li0.className = "hist-item hist-item--empty";
      li0.textContent = "Пока пусто — отправьте первый чек-ин.";
      list.appendChild(li0);
    }
  }

  // ------------------------------------------------------------------ экраны
  function showDiary() {
    el("view-bind").style.display = "none";
    el("view-diary").style.display = "block";
    renderHistory();
    updateQueueNote();
    flushQueue();
  }

  function bind(token) {
    var errEl = el("bind-error");
    errEl.textContent = "";
    if (!token) { errEl.textContent = "Вставьте код связи из QR-кода врача."; return; }
    api("/api/v1/auth/bind", { method: "POST", body: JSON.stringify({ token: token }) })
      .then(function (r) {
        if (!r.ok) {
          return r.json().then(function (d) { throw new Error(d.detail || ("HTTP " + r.status)); })
            .catch(function () { throw new Error("HTTP " + r.status); });
        }
        return r.json();
      })
      .then(function (d) {
        STORE.session = { access: d.access_token, refresh: d.refresh_token, sub: d.principal_sub };
        showDiary();
      })
      .catch(function (e) {
        errEl.textContent = "Привязка не удалась: " + (e.message || e) +
          ". Код одноразовый: попросите врача сгенерировать новый QR.";
      });
  }

  function currentCheckinPayload() {
    var item9 = el("select-item9").value;
    var cssrs = el("select-cssrs").value;
    return {
      event_uuid: uuid(),
      phq9_total: 0,                    // итоговый PHQ-9 вводит врач на приёме; п.9 — ниже
      phq9_item9: item9 === "" ? null : parseInt(item9, 10),
      gad7_total: parseInt(el("input-gad7").value, 10),
      cssrs: cssrs === "" ? null : parseInt(cssrs, 10),
      took_medication: el("btn-dose-yes").classList.contains("seg--active")
    };
  }

  function renderCheckinResult(resp) {
    var box = el("checkin-result");
    box.innerHTML = "";
    if (resp.status === "DUPLICATE") {
      box.className = "notice notice--info";
      box.textContent = "Этот чек-ин уже учтён (защита от дублей при повторной отправке).";
      return;
    }
    if (resp.suicide_item_missing) {
      var warn = document.createElement("div");
      warn.className = "notice notice--warn";
      warn.textContent = "Вы не ответили на вопрос о мыслях причинить себе вред. " +
        "Он важен для безопасности — пожалуйста, выберите ответ и отправьте ещё раз: " +
        "врач видит «риск не оценён», а не «нулевые мысли».";
      box.appendChild(warn);
    }
    if (resp.critical) {
      var crit = document.createElement("div");
      crit.className = "notice notice--critical";
      crit.textContent = "Ваш лечащий врач уведомлён и свяжется с вами. Если вам тяжело прямо сейчас — " +
        "не ждите: обратитесь к врачу или в экстренные службы. Не меняйте терапию самостоятельно.";
      box.appendChild(crit);
    } else if (!resp.suicide_item_missing) {
      var ok = document.createElement("div");
      ok.className = "notice notice--ok";
      ok.textContent = "Чек-ин передан вашему врачу. Спасибо, что заполняете дневник регулярно.";
      box.appendChild(ok);
    }
  }

  function submitCheckin() {
    var errEl = el("checkin-error");
    errEl.textContent = "";
    var payload = currentCheckinPayload();
    withRefresh(function (t) {
      return api("/api/v1/patient/checkin", { method: "POST", body: JSON.stringify(payload) }, t);
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (d) { throw new Error(d.detail || ("HTTP " + r.status)); })
          .catch(function () { throw new Error("HTTP " + r.status); });
      }
      return r.json().then(function (d) { historyAdd(payload, true); return d; });
    }).then(renderCheckinResult).catch(function (e) {
      // Сети нет ИЛИ сессия безнадёжно истекла — чек-ин терять нельзя: в очередь.
      var q = queueLoad();
      q.push({ payload: payload, queued_at: new Date().toISOString() });
      queueSave(q);
      historyAdd(payload, false);
      updateQueueNote();
      errEl.textContent = "Нет связи с сервером: чек-ин сохранён локально и уйдёт врачу при восстановлении связи.";
    });
  }

  // ------------------------------------------------------------------ инициализация
  document.addEventListener("DOMContentLoaded", function () {
    var tok = QS.get("token");
    if (tok) el("bind-token").value = tok;

    el("btn-bind").addEventListener("click", function () { bind(el("bind-token").value.trim()); });

    el("btn-dose-yes").addEventListener("click", function () {
      el("btn-dose-yes").classList.add("seg--active");
      el("btn-dose-no").classList.remove("seg--active");
    });
    el("btn-dose-no").addEventListener("click", function () {
      el("btn-dose-no").classList.add("seg--active");
      el("btn-dose-yes").classList.remove("seg--active");
    });
    el("input-gad7").addEventListener("input", function (e) {
      el("lbl-gad7").textContent = e.target.value + (e.target.value === "21" || e.target.value === "0" ? " балл" : " балла(ов)");
    });
    el("btn-checkin").addEventListener("click", submitCheckin);
    el("btn-logout").addEventListener("click", function () {
      STORE.clear();
      window.location.reload();
    });

    window.addEventListener("online", flushQueue);

    // уже привязан — сразу в дневник (PWA открывается с домашнего экрана)
    if (STORE.session && STORE.session.access) showDiary();

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(function () { /* офлайн-режий не критичен */ });
    }
  });
})();
