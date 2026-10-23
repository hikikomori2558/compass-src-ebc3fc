/* frontend_emc/app.js — логика ЭМК «вендора». ЕДИНИСТВЕННАЯ задача — работать
 * как типовая поликлиническая ЭМК: журнал, карта, лаборатория, заключение.
 * Здесь НЕТ и не должно быть ничего о COMPASS: сайдбар врача — внешний слой,
 * который считывает данные с экрана этой системы (см. frontend_compass/agent.js).
 * Данные — только у собственного API вендора (backend_emc/server.py, порт 8100).
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const state = { journal: [], current: null };

  /* ================================================================ вход (локальная сессия вендора) */
  function doLogin(evt) {
    evt.preventDefault();
    const name = $("in-doctor").value;
    if (!name) return;
    localStorage.setItem("emc_session", JSON.stringify({ doctor: name, ts: Date.now() }));
    enterApp(name);
  }
  function enterApp(doctor) {
    $("screen-login").hidden = true;
    $("app").hidden = false;
    $("user-name").textContent = doctor;
    const now = new Date();
    $("user-date").textContent = now.toLocaleDateString("ru-RU") + " " +
      now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    $("f-date").value = now.toISOString().slice(0, 10);
    loadJournal();
  }
  function logout() {
    localStorage.removeItem("emc_session");
    location.reload();
  }

  /* ================================================================ журнал (API вендора) */
  async function loadJournal() {
    const r = await fetch("/api/emc/patients");
    state.journal = await r.json();
    renderJournal();
  }

  function renderJournal() {
    const q = $("f-search").value.trim().toLowerCase();
    const doc = $("f-doctor").value;
    const rows = state.journal.filter(p =>
      (!doc || p.doctor === doc) &&
      (!q || (p.fio + " " + p.ib + " " + (p.icd10 || "") + " " + (p.tag || "")).toLowerCase().includes(q)));
    const tb = $("journal-tbody");
    tb.innerHTML = "";
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      const st = p.status === "ожидает" ? "st-wait" : (p.status === "на приёме" ? "st-active" : "st-done");
      tr.innerHTML =
        "<td>" + (i + 1) + "</td>" +
        "<td>" + p.slot + "</td>" +
        "<td><b>" + p.fio + "</b></td>" +
        "<td>" + p.born + "</td>" +
        "<td>" + (p.icd10 || "—") + "</td>" +
        "<td>" + (p.tag ? "<span class='emc-chip'>" + p.tag + "</span>" : "") + "</td>" +
        "<td><span class='" + st + "'>" + p.status + "</span></td>" +
        "<td><button class='emc-btn'>Открыть карту</button></td>";
      tr.addEventListener("click", () => openCard(p.ib));
      tb.appendChild(tr);
    });
    const waiting = state.journal.filter(p => p.status === "ожидает").length;
    $("journal-stat").textContent = "Пациентов: " + state.journal.length +
      " • на приёме: " + state.journal.filter(p => p.status === "на приёме").length +
      " • ожидает: " + waiting;
  }

  /* ================================================================ карта */
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
      ["СКФ (из документа)", grab(/СКФ[^\d]{0,15}(\d+(?:[.,]\d+)?)/i), "мл/мин/1.73м²", "≥ 60"],
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
      ["C-SSRS (суицидальный риск)", cs === null ? "—" : cs + " б.", cs === null ? "—" : (Number(cs) >= 1 ? "требует реакции" : "нет риска")],
    ];
  }

  async function openCard(ib) {
    const r = await fetch("/api/emc/patient/" + encodeURIComponent(ib));
    const p = await r.json();
    state.current = p;

    $("cb-fio").textContent = p.fio;
    const age = new Date().getFullYear() - Number(p.born.slice(0, 4));
    $("cb-sex-age").textContent = (p.sex === "Ж" ? "Женский" : "Мужской") + " • " + age + " лет (" + p.born + ")";
    $("cb-ib").textContent = "ИБ № " + p.ib;
    $("cb-oms").textContent = "ОМС: " + p.oms;
    $("cb-icd").textContent = "МКБ-10: " + (p.icd10 || "не указан");
    $("cb-source").textContent = p.tag || "";

    const text = p.note_saved || p.raw_text || "";
    const stopIdx = Math.max(text.search(/Лабораторные/i), text.search(/Лаб:/i), text.search(/Шкалы/i));
    $("insp-complaints").textContent = (stopIdx > 0 ? text.slice(0, stopIdx).replace(/^[^:]*:\s*/, "") : text)
      .replace(/^[A-Z]\d{2}(?:\.\d)?[.\s]*/i, "") || "—";
    $("insp-anamnesis").textContent = "Данные перенесены из первичного документа (выписка/осмотр ф. 025/у).";
    $("insp-text").value = text;
    $("insp-doctor").textContent = p.doctor || "врач";
    $("insp-date").textContent = new Date().toLocaleDateString("ru-RU");

    $("labs-tbody").innerHTML = parseLabs(text).map(l =>
      "<tr><td>" + l[0] + "</td><td>" + (l[1] === null ? "не указано" : l[1]) + "</td><td>" + l[2] + "</td><td>" + l[3] + "</td></tr>").join("");

    const known = [["метопролол", "Метопролол", "50 мг/сут"], ["дулоксетин", "Дулоксетин", "60 мг/сут"],
      ["сертралин", "Сертралин", "100 мг/сут"], ["эсциталопрам", "Эсциталопрам", "10 мг/сут"],
      ["флуконазол", "Флуконазол", "150 мг/сут"], ["ципрофлоксацин", "Ципрофлоксацин", "500 мг 2 р/сут"]];
    const meds = known.filter(k => text.toLowerCase().includes(k[0]));
    $("meds-tbody").innerHTML = meds.length ? meds.map(k =>
      "<tr><td>" + k[1] + "</td><td>" + k[2] + "</td><td>—</td><td>" + (p.doctor || "") + "</td></tr>").join("")
      : "<tr><td colspan='4'>Активных сопутствующих назначений не найдено</td></tr>";

    $("scales-tbody").innerHTML = parseScales(text).map(s =>
      "<tr><td>" + s[0] + "</td><td>" + s[1] + "</td><td>" + s[2] + "</td></tr>").join("");

    switchView("card");
    $("tab-card").hidden = false;
    switchCardTab("inspect");
  }

  async function saveNote() {
    if (!state.current) return;
    const r = await fetch("/api/emc/patient/" + encodeURIComponent(state.current.ib) + "/note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("insp-text").value }),
    });
    if (r.ok) {
      alert("Заключение сохранено в ЭМК (подписано ЭЦП вендора).");
    } else {
      alert("Ошибка сохранения: HTTP " + r.status);
    }
  }

  /* ================================================================ навигация */
  function switchView(name) {
    document.querySelectorAll(".emc-view").forEach(v => v.classList.remove("active"));
    $("view-" + name).classList.add("active");
    document.querySelectorAll("#emc-tabs button").forEach(b =>
      b.classList.toggle("active", b.dataset.view === name));
  }
  function switchCardTab(name) {
    document.querySelectorAll(".emc-card-tab").forEach(v => v.classList.remove("active"));
    $("card-tab-" + name).classList.add("active");
    document.querySelectorAll("#card-tabs button").forEach(b =>
      b.classList.toggle("active", b.dataset.tab === name));
  }

  /* ================================================================ init */
  document.addEventListener("DOMContentLoaded", () => {
    $("form-login").addEventListener("submit", doLogin);
    $("btn-logout").addEventListener("click", logout);
    $("f-search").addEventListener("input", renderJournal);
    $("f-doctor").addEventListener("change", renderJournal);
    document.querySelectorAll("#emc-tabs button").forEach(b =>
      b.addEventListener("click", () => switchView(b.dataset.view)));
    document.querySelectorAll("#card-tabs button").forEach(b =>
      b.addEventListener("click", () => switchCardTab(b.dataset.tab)));
    $("btn-to-journal").addEventListener("click", () => switchView("journal"));
    $("btn-save-note").addEventListener("click", saveNote);

    try {
      const s = JSON.parse(localStorage.getItem("emc_session") || "null");
      if (s && s.doctor) enterApp(s.doctor);
    } catch (e) { /* сессии нет — покажем вход */ }
  });
})();
