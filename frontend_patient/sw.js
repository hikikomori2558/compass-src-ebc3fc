/*
 * frontend_patient/sw.js — service worker PWA-дневника.
 *
 * Стратегия: cache-first ТОЛЬКО для оболочки (html/css/js/иконки — они версионируются
 * именем кэша). Всё остальное (включая API бэкенда) — сразу в сеть: клинические
 * данные между пациентом и МО не должны оседать в кэше браузера дольше сессии.
 */
var CACHE = "compass-patient-shell-v1";
var SHELL = [
  "./",
  "index.html",
  "app.js",
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
  e.respondWith(
    caches.match(e.request).then(function (hit) {
      if (hit) return hit;
      return fetch(e.request).then(function (resp) {
        if (resp.ok && SHELL.some(function (s) { return url.pathname.endsWith(s.replace("./", "")); })) {
          var copy = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
        }
        return resp;
      });
    })
  );
});
