/*
 * frontend_patient/sw.js — service worker PWA-дневника.
 *
 * Стратегия: cache-first ТОЛЬКО для оболочки (html/css/js/иконки — они версионируются
 * именем кэша). Всё остальное (включая API бэкенда) — сразу в сеть: клинические
 * данные между пациентом и МО не должны оседать в кэше браузера дольше сессии.
 */
var CACHE = "compass-patient-shell-v5-sage"; // v4: Duolingo-механика, без эмодзи
var SHELL = [
  "./",
  "index.html",
  "app.js",
  "survey.js",
  "styles.css",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () {
    return self.skipWaiting();
  }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;      // API — только сеть
  if (e.request.method !== "GET") return;
  // HTML/JS/CSS — NETWORK-FIRST (кэш только офлайн-фоллбэк): устаревший код
  // невидимо ломал PWA после каждого обновления (было 23–24.10). Иконки/манифест —
  // cache-first (меняются только с новой версией SW).
  var shellOnly = SHELL.some(function (s) { return url.pathname.endsWith(s.replace("./", "")); });
  var isCode = /\.(html|js|css)($|\?)/.test(url.pathname) || url.pathname.endsWith("/");
  e.respondWith(
    fetch(e.request).then(function (resp) {
      if (resp.ok && shellOnly) {
        var copy = resp.clone();
        caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
      }
      return resp;
    }).catch(function () {
      return caches.match(e.request).then(function (hit) {
        if (hit) return hit;
        if (isCode) {
          return caches.match("./index.html").then(function (idx) {
            return idx || new Response("Офлайн", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
          });
        }
        return new Response("", { status: 503 });
      });
    })
  );
});

/* ==== Retention 2.0: два слота напоминаний (Этап 6) ====
 * Без push-сервера (нет зависимости от внешних сервисов): пока PWA открыта,
 * таймер шлёт локальные уведомления; periodicSync — best effort. Текст —
 * контекстный по неделе терапии (КР МЗ РФ: точки 0/2/4/8 недель). */
var SLOT_TEXT = {
  morning: "Время приёма Авиандр® — за 30 минут до еды.",
  evening: "Пара минут на дневник: сон, силы, самочувствие — врач увидит тренд.",
};
self.addEventListener("message", function (e) {
  var d = e.data || {};
  if (d.type === "SCHEDULE_REMINDERS") {
    self.__slots = d.slots || { morning: "09:00", evening: "20:00" };
    self.__week = d.week || 1;
  }
});
function weekNote(week) {
  if (week <= 1) return "Неделя 1 — период адаптации, реакции обычно уходят к 5–7 дню.";
  if (week === 2) return "Неделя 2 — эффект начинает проявляться. Регулярность важнее идеальности.";
  if (week === 4) return "Неделя 4 — контрольная точка: врач оценивает динамику.";
  return "";
}
function notify(slot) {
  var note = weekNote(self.__week || 1);
  self.registration.showNotification("COMPASS", {
    body: (SLOT_TEXT[slot] || "") + (note ? " " + note : ""),
    tag: "compass-" + slot,
    silent: false,
  });
}
function tick() {
  var slots = self.__slots || { morning: "09:00", evening: "20:00" };
  var now = new Date();
  var hhmm = ("0" + now.getHours()).slice(-2) + ":" + ("0" + now.getMinutes()).slice(-2);
  ["morning", "evening"].forEach(function (slot) {
    if (slots[slot] === hhmm) notify(slot);
  });
}
setInterval(tick, 55 * 1000);
self.addEventListener("periodicsync", function (e) {
  if (e.tag === "compass-reminders") tick();
});
