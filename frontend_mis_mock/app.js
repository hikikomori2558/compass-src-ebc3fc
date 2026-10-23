/* frontend_mis_mock/app.js — логика МИС-стенда «АИС Поликлиника» (демо, жанр типовой
 * российской поликлинической ЭМК). Клиентские парсинги лаборатории/шкал здесь — только
 * для ОТОБРАЖЕНИЯ в карте; клинические решения всегда считает серверный safe-pipeline
 * (/api/v1/compass-bio/ehr/analyze-and-route) — это принципиально для честности демо.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const state = {
    journal: [],
    current: null,          // текущая запись журнала (карта открыта)
    ws: null,
    alertIds: [],           // неподтверждённые алерты [{alert_id,...}]
    currentAlert: null,
  };

  /* ================================================================ вход */
  async function doLogin(evt) {
    evt.preventDefault();
    const err = $("login-err");
    err.hidden = true;
    $("btn-login").disabled = true;
    $("btn-login").textContent = "Проверка…";
    try {
      const s = await CompassAPI.login($("in-login").value.trim(), $("in-password").value);
      $("user-name").textContent = s.principal_sub === "" ? "" : ($("in-login").value.trim());
      enterApp(s);
    } catch (e) {
      err.textContent = e.message || String(e);
      err.hidden = false;
    } finally {
      $("btn-login").disabled = false;
      $("btn-login").textContent = "Войти в систему";
    }
  }

  function enterApp(session) {
    $("screen-login").hidden = true;
    $("app").hidden = false;
    $("user-name").textContent = $("in-login").value.trim() || "врач";
    const now = new Date();
    $("user-date").textContent = now.toLocaleDateString("ru-RU") + " " +
      now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    $("f-date").value = now.toISOString().slice(0, 10);
    loadJournal();
    refreshAlerts();
    connectWS();
    setInterval(refreshAlerts, 20000);   // страховочный поллинг поверх WS
  }

  function logout() {
    try { if (state.ws) state.ws.close(); } catch (e) { /* уже закрыт */ }
    CompassAPI.dropSession();
    location.reload();
  }

  /* ================================================================ журнал */
  async function loadJournal() {
    const r = await fetch("data/patients_today.json");
    state.journal = await r.json();
    renderJournal();
  }

  function journalFiltered() {
    const q = $("f-search").value.trim().toLowerCase();
    const doc = $("f-doctor").value;
    return state.journal.filter(p =>
      (!doc || p.doctor === doc) &&
      (!q || (p.fio + " " + p.ib + " " + (p.icd10 || "") + " " + (p.tag || "")).toLowerCase().includes(q))
    );
  }

  function renderJournal() {
    const rows = journalFiltered();
    const tb = $("journal-tbody");
    tb.innerHTML = "";
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + (i + 1) + "</td>" +
        "<td class='mono'>" + p.slot + "</td>" +
        "<td><b>" + p.fio + "</b></td>" +
        "<td>" + p.born + "</td>" +
        "<td>" + (p.icd10 || "—") + "</td>" +
        "<td>" + tagChip(p) + "</td>" +
        "<td><span class='st-" + statusClass(p.status) + "'>" + p.status + "</span></td>" +
        "<td><button class='mis-btn'>Открыть карту</button></td>";
      tr.addEventListener("click", () => openCard(p));
      tb.appendChild(tr);
    });
    const waiting = state.journal.filter(p => p.status === "ожидает").length;
    $("journal-stat").textContent =
      "Пациентов: " + state.journal.length + " • на приёме: " +
      state.journal.filter(p => p.status === "на приёме").length + " • ожидает: " + waiting;
  }

  function tagChip(p) {
    let cls = "mis-chip";
    if (p.source === "webinar") cls += " webinar";
    if ((p.tag || "").includes("CONTRA")) cls += " contra";
    return "<span class='" + cls + "'>" + (p.tag || "") + "</span>";
  }
  function statusClass(s) {
    return s === "ожидает" ? "wait" : (s === "на приёме" ? "active" : "done");
  }

  /* ================================================================ карта */
  /* Лёгкий разбор текста — только чтобы заполнить вкладки отображения. */
  function parseLabs(text) {
    const grab = (re) => { const m = text.match(re); return m ? m[1] : null; };
    return [
      ["Креатинин сыворотки", grab(/креат(?:инин)?[^\d]{0,20}(\d+(?:[.,]\d+)?)/i), "мкмоль/л", "62–115"],
      ["АЛТ", grab(/АЛТ[^\d]{0,10}(\d+(?:[.,]\d+)?)/i), "Ед/л", "< 41"],
      ["АСТ", grab(/АСТ[^\d]{0,10}(\d+(?:[.,]\d+)?)/i), "Ед/л", "< 40"],
      ["Билирубин общий", grab(/билирубин[^\d]{0,10}(\d+(?:[.,]\d+)?)/i), "мкмоль/л", "3–21"],
      ["Альбумин", grab(/альбумин[^\d]{0,10}(\d+(?:[.,]\d+)?)/i), "г/л", "35–50"],
      ["МНО", grab(/МНО[^\d]{0,8}(\d+(?:[.,]\d+)?)/i), "", "0.8–1.2"],
      ["ТТГ", grab(/ТТГ[^\d]{0,10}(\d+(?:[.,]\d+)?)/i), "мМЕ/л", "0.4–4.0"],
      ["СКФ (в тексте ЭМК)", grab(/СКФ[^\d]{0,15}(\d+(?:[.,]\d+)?)/i), "мл/мин/1.73м²", "≥ 60"],
    ];
  }
  function parseScales(text) {
    const grab = (re) => { const m = text.match(re); return m ? m[1] : null; };
    const g7 = grab(/GAD-?7[^\d]{0,6}(\d{1,2})/i);
    const p9 = grab(/PHQ-?9[^\d]{0,6}(\d{1,2})/i);
    const cs = grab(/C-?SSRS[^\d]{0,6}(\d{1,2})/i);
    const interp = (v, a, b) => v === null ? "—" : (Number(v.replace(",", ".")) >= b ? "тяжёлая степень" : (Number(v) >= a ? "умеренная" : "лёгкая/минимальная"));
    return [
      ["GAD-7 (тревога)", g7 === null ? "—" : g7 + " б.", interp(g7, 10, 15)],
      ["PHQ-9 (депрессия)", p9 === null ? "—" : p9 + " б.", interp(p9, 10, 15)],
      ["C-SSRS (суицидальный риск)", cs === null ? "—" : cs + " б.", cs === null ? "—" : (Number(cs) >= 1 ? "требует реакции!" : "нет риска")],
    ];
  }

  function openCard(p) {
    state.current = p;
    CompassSidebar.reset(p);

    $("cb-fio").textContent = p.fio;
    const age = new Date().getFullYear() - Number(p.born.slice(0, 4));
    $("cb-sex-age").textContent = (p.sex === "Ж" ? "Женский" : "Мужской") + " • " + age + " лет (" + p.born + ")";
    $("cb-ib").textContent = "ИБ № " + p.ib;
    $("cb-oms").textContent = "ОМС: " + p.oms;
    $("cb-icd").textContent = "МКБ-10: " + (p.icd10 || "не указан");
    $("cb-source").textContent = p.tag || "";

    /* вкладка осмотра: жалобы — фрагмент до лаборатории, заключение — весь текст */
    const text = p.raw_text || "";
    const stopIdx = Math.max(text.search(/Лабораторные/i), text.search(/Лаб:/i), text.search(/Шкалы/i));
    const complaints = stopIdx > 0 ? text.slice(0, stopIdx).replace(/^[^:]*:\s*/, "") : text;
    $("insp-complaints").textContent = complaints || "—";
    $("insp-anamnesis").textContent =
      "Данные перенесены из импортированной истории (шаблон " + (p.source === "webinar" ? "осмотр ф. 025/у" : "ЭМК") + "). Полный текст — во вкладке заключения.";
    $("insp-text").value = text;
    $("import-text").value = text;

    $("labs-tbody").innerHTML = parseLabs(text).map(l =>
      "<tr><td>" + l[0] + "</td><td>" + (l[1] === null ? "<span class='mis-chip contra'>не указано</span>" : l[1]) +
      "</td><td>" + l[2] + "</td><td>" + l[3] + "</td></tr>").join("");

    $("meds-tbody").innerHTML = medRows(text);

    $("scales-tbody").innerHTML = parseScales(text).map(s =>
      "<tr><td>" + s[0] + "</td><td>" + s[1] + "</td><td>" + s[2] + "</td></tr>").join("");

    switchView("card");
    $("tab-card").hidden = false;
    switchCardTab("inspect");
  }

  function medRows(text) {
    const known = [
      ["метопролол", "Метопролол", "50 мг/сут"],
      ["дулоксетин", "Дулоксетин", "60 мг/сут"],
      ["сертралин", "Сертралин", "100 мг/сут"],
      ["эсциталопрам", "Эсциталопрам", "10 мг/сут"],
      ["флуконазол", "Флуконазол", "150 мг/сут"],
      ["ципрофлоксацин", "Ципрофлоксацин", "500 мг 2 р/сут"],
    ];
    const found = known.filter(k => text.toLowerCase().includes(k[0]));
    if (!found.length) return "<tr><td colspan='4'>Активных сопутствующих назначений не найдено</td></tr>";
    return found.map(k =>
      "<tr><td>" + k[1] + "</td><td>" + k[2] + "</td><td>—</td><td>" +
      (state.current ? state.current.doctor : "") + "</td></tr>").join("");
  }

  function pushToCompass() {
    if (!state.current) return;
    CompassSidebar.analyze($("insp-text").value);
  }
  function importAndAnalyze() {
    if (!state.current) return;
    $("insp-text").value = $("import-text").value;
    CompassSidebar.analyze($("import-text").value);
  }

  /* ================================================================ навигация */
  function switchView(name) {
    document.querySelectorAll(".mis-view").forEach(v => v.classList.remove("active"));
    $("view-" + name).classList.add("active");
    document.querySelectorAll("#mis-tabs button").forEach(b =>
      b.classList.toggle("active", b.dataset.view === name));
  }
  function switchCardTab(name) {
    document.querySelectorAll(".mis-card-tab").forEach(v => v.classList.remove("active"));
    $("card-tab-" + name).classList.add("active");
    document.querySelectorAll("#card-tabs button").forEach(b =>
      b.classList.toggle("active", b.dataset.tab === name));
  }

  /* ================================================================ алерты */
  async function refreshAlerts() {
    if (!CompassAPI.session) return;
    const r = await CompassAPI.call("/api/v1/alerts/unconfirmed");
    if (!r.ok) return;
    const data = await r.json();
    state.alertIds = data.alerts || [];
    const badge = $("alerts-badge");
    badge.hidden = data.count === 0;
    badge.textContent = String(data.count);
    const banner = $("alert-banner");
    if (data.count > 0) {
      const a = data.alerts[0];
      $("alert-banner-title").textContent = "Критический алерт (" + data.count + " неподтверждённых)";
      $("alert-banner-sub").textContent = " " + a.category + " • " + (a.reasons || []).join("; ").slice(0, 160) +
        " • " + new Date(a.created_at).toLocaleTimeString("ru-RU");
      state.currentAlert = a;
      banner.hidden = false;
    } else {
      banner.hidden = true;
      state.currentAlert = null;
    }
  }

  function connectWS() {
    const s = CompassAPI.session;
    if (!s) return;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    const host = CompassAPI.base ? CompassAPI.base.replace(/^https?:\/\//, "") : location.host;
    const url = proto + host + "/ws/alerts/" + s.principal_sub + "?token=" + s.access_token;
    const el = $("ws-state");
    try { if (state.ws) state.ws.close(); } catch (e) { /* ignore */ }
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => { el.textContent = "WS: online"; el.className = "mis-ws-state on"; };
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.kind === "CRITICAL_PING") refreshAlerts(); // в канале пустой пинг; клиника — по REST
    };
    ws.onclose = () => {
      el.textContent = "WS: offline"; el.className = "mis-ws-state off";
      setTimeout(connectWS, 5000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  }

  function confirmAlertModal() {
    if (!state.currentAlert) return;
    const a = state.currentAlert;
    $("confirm-alert-info").textContent =
      "Алерт " + a.alert_id + " • " + a.category + " • " + (a.reasons || []).join("; ");
    $("modal-confirm-alert").hidden = false;
  }
  async function submitAlertConfirm() {
    const a = state.currentAlert;
    if (!a) return;
    const action = $("confirm-action").value.trim();
    if (action.length < 3) { alert("Опишите, что сделано (минимум 3 символа)"); return; }
    const r = await CompassAPI.call("/api/v1/alerts/" + a.alert_id + "/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_taken: action }),
    });
    if (r.ok) {
      $("modal-confirm-alert").hidden = true;
      refreshAlerts();
    } else {
      alert("Не удалось подтвердить: HTTP " + r.status);
    }
  }

  /* ================================================================ 057/у-04 */
  function form057() {
    if (!state.current) return;
    const p = state.current;
    const res = CompassSidebar.lastResult;
    const overall = res ? res.overall_status : "—";
    const routing = res && res.compass_decision && res.compass_decision.routing_and_alerting || {};
    $("f057-body").innerHTML =
      "<h4>НАПРАВЛЕНИЕ НА КОНСУЛЬТАЦИЮ ПАЦИЕНТА</h4>" +
      "<div class='f057-org'>Форма № 057/у-04, утв. приказом Минздрава России № 646н (демо-бланк стенда)</div>" +
      "<table>" +
      f057row("Дата формирования", new Date().toLocaleString("ru-RU")) +
      f057row("Наименование медицинской организации", "ГБУЗ «Городская поликлиника № 158 ДЗМ» (демо-стенд)") +
      f057row("ФИО пациента", p.fio + " • " + (p.sex === "Ж" ? "Ж" : "М") + " • " + p.born + " • ИБ № " + p.ib) +
      f057row("Код диагноза по МКБ-10", p.icd10 || "—") +
      f057row("Цель направления", "Консультация врача-психиатра" + (routing.routing_category ? " (категория маршрутизации COMPASS: " + routing.routing_category + ")" : "")) +
      f057row("Статус COMPASS на момент формирования", overall) +
      f057row("Направил", $("user-name").textContent + " • " + (p.doctor || "врач")) +
      "</table>" +
      "<div style='font-size:10px;color:#5a6a78'>Сформировано на демо-стенде COMPASS-HYBRID v2.6 (команда 20-П «Rapira»). Пациенты синтетические.</div>";
    $("modal-057").hidden = false;
  }
  function f057row(k, v) { return "<tr><td class='k'>" + k + "</td><td>" + v + "</td></tr>"; }

  function directions() {
    try { return JSON.parse(localStorage.getItem("mis_directions") || "[]"); }
    catch (e) { return []; }
  }
  function save057() {
    if (!state.current) return;
    const list = directions();
    list.unshift({
      num: list.length + 1,
      ts: new Date().toLocaleString("ru-RU"),
      fio: state.current.fio,
      icd10: state.current.icd10 || "—",
      aim: "Консультация психиатра",
      status: CompassSidebar.lastResult ? CompassSidebar.lastResult.overall_status : "—",
    });
    localStorage.setItem("mis_directions", JSON.stringify(list));
    $("modal-057").hidden = true;
    renderDirections();
    switchView("directions");
  }
  function renderDirections() {
    const list = directions();
    $("dir-count").textContent = String(list.length);
    $("directions-tbody").innerHTML = list.length ? list.map(d =>
      "<tr><td>" + d.num + "</td><td>" + d.ts + "</td><td><b>" + d.fio + "</b></td><td>" +
      d.icd10 + "</td><td>" + d.aim + "</td><td>" + d.status + "</td>" +
      "<td><button class='mis-btn'>Печать</button></td></tr>").join("")
      : "<tr><td colspan='7'>Направлений пока не сформировано</td></tr>";
  }

  /* ================================================================ init */
  document.addEventListener("DOMContentLoaded", () => {
    $("form-login").addEventListener("submit", doLogin);
    $("btn-logout").addEventListener("click", logout);
    $("f-search").addEventListener("input", renderJournal);
    $("f-doctor").addEventListener("change", renderJournal);
    document.querySelectorAll("#mis-tabs button").forEach(b =>
      b.addEventListener("click", () => switchView(b.dataset.view)));
    document.querySelectorAll("#card-tabs button").forEach(b =>
      b.addEventListener("click", () => switchCardTab(b.dataset.tab)));
    $("btn-to-journal").addEventListener("click", () => switchView("journal"));
    $("btn-push-compass").addEventListener("click", pushToCompass);
    $("btn-import-analyze").addEventListener("click", importAndAnalyze);
    $("btn-save-inspect").addEventListener("click", () => {
      state.current.raw_text = $("insp-text").value;
      alert("Заключение сохранено в карте (демо-стенд).");
    });

    $("btn-alerts").addEventListener("click", refreshAlerts);
    $("btn-alert-open").addEventListener("click", () => {
      refreshAlerts();
      if (state.alertIds.length) alert("Алертов неподтверждённых: " + state.alertIds.length +
        "\nПодтвердите реакцию кнопкой «Подтвердить реакцию…».");
    });
    $("btn-alert-confirm").addEventListener("click", confirmAlertModal);
    $("btn-confirm-ok").addEventListener("click", submitAlertConfirm);
    $("btn-confirm-cancel").addEventListener("click", () => $("modal-confirm-alert").hidden = true);

    $("btn-form-057").addEventListener("click", form057);
    $("btn-057-save").addEventListener("click", save057);
    $("btn-057-print").addEventListener("click", () => window.print());
    $("btn-057-cancel").addEventListener("click", () => $("modal-057").hidden = true);

    renderDirections();

    /* восстановление сессии: врач уже входил — сразу в журнал */
    if (CompassAPI.restoreSession()) {
      $("in-login").value = localStorage.getItem("mis_last_login") || $("in-login").value;
      enterApp(CompassAPI.session);
    }
    $("form-login").addEventListener("submit", () =>
      localStorage.setItem("mis_last_login", $("in-login").value.trim()), { once: true });
  });
})();
