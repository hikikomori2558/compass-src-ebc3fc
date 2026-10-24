/* frontend_compass/agent.js — экранный агент COMPASS.
 *
 * Загружается В СТРАНИЦУ ЭМК букмарклетом (мини-версия браузерного расширения —
 * тот же механизм, каким реальный виджет встраивается поверх проприетарной МИС).
 * ЭМК о нём не знает: ни одного скрипта COMPASS в файлах ЭМК нет.
 *
 * Что делает агент:
 *   1. считывает ДАННЫЕ С ЭКРАНА открытой карты пациента (видимый DOM — как
 *      скрин-ридер: ФИО, диагноз, жалобы, заключение, лаборатория, шкалы);
 *   2. подсвечивает считанные области (врач видит, что именно прочитано);
 *   3. открывает поверх ЭМК iframe-сайдбар врача (origin COMPASS) и передаёт
 *      ему экранный текст postMessage'ом.
 *
 * Клинических решений агент не принимает и клинику никуда не отправляет:
 * анализ делает сайдбар напрямую через авторизованный API COMPASS.
 */
(function () {
  "use strict";
  if (window.__compassAgentInstalled) return; // идемпотентность
  window.__compassAgentInstalled = true;

  var PANEL_W = 460;

  function $(id) { return document.getElementById(id); }

  /* -------- 1. ЧТЕНИЕ ЭКРАНА: только то, что реально видно в карте --------- */
  function readScreen() {
    var fio = $("cb-fio");
    var text = $("insp-text");
    if (!fio || !fio.offsetParent) return null; // карта пациента не открыта

    var sexAge = ($("cb-sex-age") || {}).textContent || "";
    var icd = ($("cb-icd") || {}).textContent || "";
    var complaints = ($("insp-complaints") || {}).textContent || "";
    var conclusion = text ? text.value : "";

    var labs = [];
    var labRows = document.querySelectorAll("#labs-tbody tr");
    labRows.forEach(function (tr) {
      var c = tr.querySelectorAll("td");
      if (c.length >= 2 && c[1].textContent.trim() && c[1].textContent.indexOf("не указано") === -1) {
        labs.push(c[0].textContent.trim() + ": " + c[1].textContent.trim() + " " + (c[2] ? c[2].textContent.trim() : ""));
      }
    });

    var scales = [];
    document.querySelectorAll("#scales-tbody tr").forEach(function (tr) {
      var c = tr.querySelectorAll("td");
      if (c.length >= 2 && c[1].textContent.trim() && c[1].textContent.trim() !== "—") {
        scales.push(c[0].textContent.trim().replace(/\s*\(.*\)/, "") + " = " + c[1].textContent.trim().replace(" б.", " б."));
      }
    });

    /* Экранный документ: собираем ровно то, что врач видит на мониторе */
    var screenText = "Осмотр невролога (экранные данные ЭМК, ф. 025/у). " +
      sexAge.replace(" • ", ", ").replace(/\s*\(\d{4}-\d{2}-\d{2}\)/, "") + ". " +
      icd.replace("МКБ-10: ", "Диагноз по МКБ-10: ") + ". " +
      "Жалобы: " + complaints.replace(/\s+/g, " ").trim() + " " +
      (conclusion ? "Заключение: " + conclusion.replace(/\s+/g, " ").trim() + " " : "") +
      (labs.length ? "Лабораторные данные: " + labs.join(", ") + ". " : "") +
      (scales.length ? "Шкалы: " + scales.join(", ") + "." : "");

    return {
      ib: (($("cb-ib") || {}).textContent || "").replace("ИБ № ", "").trim(),
      fio: fio.textContent.trim(),
      screenText: screenText,
    };
  }

  /* -------- 2. ПОДСВЕТКА СЧИТАННОГО (врач видит процесс чтения экрана) ----- */
  function flashScanned() {
    var ids = ["cb-fio", "cb-icd", "insp-complaints", "insp-text", "labs-tbody", "scales-tbody"];
    ids.forEach(function (id) {
      var el = $(id);
      if (!el || !el.offsetParent) return;
      var prev = el.style.outline;
      el.style.outline = "2px solid #0f62fe";
      el.style.outlineOffset = "2px";
      setTimeout(function () { el.style.outline = prev; }, 1600);
    });
  }

  /* -------- 3. ОВЕРЛЕЙ-САЙДБАР ПОВЕРХ ЭМК ---------------------------------- */
  var frame = null, bubble = null, lastPayload = null, ready = false;

  function ensureFrame(base) {
    if (frame) return;
    var host = document.createElement("div");
    host.id = "compass-agent-host";
    host.style.cssText = "position:fixed;top:0;right:0;bottom:0;width:" + PANEL_W +
      "px;z-index:2147483000;box-shadow:-18px 0 44px -14px rgba(9,22,40,.35);";
    frame = document.createElement("iframe");
    frame.src = base + "/static/compass/sidebar.html?v=" + Date.now(); // минуя любые кэши
    frame.style.cssText = "width:100%;height:100%;border:0;background:#fff;";
    host.appendChild(frame);
    document.body.appendChild(host);

    bubble = document.createElement("button");
    bubble.type = "button";
    bubble.textContent = "COMPASS";
    bubble.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483001;" +
      "height:38px;padding:0 18px;border:0;border-radius:999px;cursor:pointer;" +
      "background:#0f62fe;color:#fff;font:700 13px system-ui,sans-serif;" +
      "box-shadow:0 10px 24px -8px rgba(9,54,131,.5);display:none;";
    bubble.addEventListener("click", function () { toggle(base); });
    document.body.appendChild(bubble);

    window.addEventListener("message", function (e) {
      var d = e.data || {};
      if (d.type === "COMPASS_READY" && frame) {
        ready = true;
        if (lastPayload) frame.contentWindow.postMessage(lastPayload, "*");
      }
      if (d.type === "COMPASS_CLOSE") toggle(base, true);
    });
  }

  function toggle(base, forceHide) {
    var host = document.getElementById("compass-agent-host");
    if (!host) return;
    var open = host.style.display !== "none";
    var show = forceHide ? false : !open;
    host.style.display = show ? "block" : "none";
    bubble.style.display = show ? "none" : "block";
  }

  /* -------- тост-подсказка поверх ЭМК (вместо системного alert) ---------- */
  function toast(title, lines, ms) {
    var old = document.getElementById("compass-agent-toast");
    if (old) old.remove();
    var t = document.createElement("div");
    t.id = "compass-agent-toast";
    t.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:24px;" +
      "z-index:2147483002;max-width:430px;background:#fff;border:1px solid #d0d7de;" +
      "border-left:4px solid #0f62fe;border-radius:10px;box-shadow:0 16px 40px -12px rgba(9,22,40,.3);" +
      "padding:12px 16px;font:13px/1.5 system-ui,sans-serif;color:#161616;text-align:left;";
    var h = document.createElement("div");
    h.style.cssText = "font-weight:700;margin-bottom:4px;";
    h.textContent = title;
    t.appendChild(h);
    (lines || []).forEach(function (l) {
      var d = document.createElement("div");
      d.style.cssText = "color:#57606a;font-size:12px;";
      d.textContent = l;
      t.appendChild(d);
    });
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, ms || 4200);
  }

  /* -------- точка входа: window.__compassAgent(baseUrl) --------------------- */
  window.__compassAgent = function (base) {
    var payload = readScreen();
    if (!payload) {
      toast("COMPASS: карта пациента не открыта", [
        "1. Откройте пациента в «Журнале приёма» (кнопка «Открыть карту»).",
        "2. Нажмите закладку «COMPASS ▸» ещё раз — агент читает экран карты.",
      ], 6000);
      return;
    }
    lastPayload = { type: "COMPASS_SCREEN", emcOrigin: location.origin, ts: Date.now(), payload: payload };
    flashScanned();
    ensureFrame(base);
    if (ready && frame) frame.contentWindow.postMessage(lastPayload, "*");
    toast("COMPASS: экран считан", [
      "Пациент: " + payload.fio + " • ИБ № " + payload.ib,
      "Разбор открывается в панели справа. Подтвердите пункты врача в ней.",
    ], 4000);
  };
})();
