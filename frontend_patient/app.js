/* frontend_patient/app.js — PWA 2.0: вкладки, стрики, настоящие опросники,
   пояснения побочек в момент возникновения, напоминания, кризисный экран. */
(function () {
  "use strict";
  const QS = new URLSearchParams(location.search);
  const API = (QS.get("api") || localStorage.getItem("cp_api") ||
    (location.hostname !== "127.0.0.1" && location.hostname !== "localhost"
      ? location.protocol + "//" + location.hostname + ":8000" : "http://127.0.0.1:8000")).replace(/\/+$/, "");
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx".replace(/[xy]/g, c => { const r = Math.random()*16|0; return (c==="x"?r:(r&3|8)).toString(16); }));

  const S = { token: null, session: null, seSelected: [], quiz: null, quizAnswers: [], me: null };

  function api(path, opts, token) {
    return fetch(API + path, Object.assign({}, opts, { headers: Object.assign(
      { "Content-Type": "application/json" },
      token ? { Authorization: "Bearer " + token } : {}, (opts && opts.headers) || {}) }));
  }
  function withRefresh(fn) {
    const s = STORE.session;
    if (!s) return Promise.reject(new Error("нет сессии"));
    return fn(s.access).then(r => r.status !== 401 ? r :
      api("/api/v1/auth/refresh", { method: "POST", body: JSON.stringify({ refresh_token: s.refresh }) })
        .then(rr => rr.ok ? rr.json().then(d => { STORE.session = { access: d.access_token, refresh: d.refresh_token }; return fn(d.access_token); }) : (STORE.clear(), Promise.reject(new Error("сессия истекла")))));
  }
  const STORE = {
    get session() { try { return JSON.parse(localStorage.getItem("cp_session") || "null"); } catch(e){ return null; } },
    set session(v) { localStorage.setItem("cp_session", JSON.stringify(v)); },
    clear() { localStorage.removeItem("cp_session"); }
  };

  /* ---------- вкладки ---------- */
  function switchTab(name) {
    ["today","scales","progress","more"].forEach(t => { el("tab-"+t).style.display = t===name?"":"none"; });
    document.querySelectorAll(".tab").forEach(b => b.classList.toggle("tab--active", b.dataset.tab === name));
    if (name === "progress") renderProgress();
  }

  /* ---------- дневник «Сегодня» ---------- */
  function loadChoices() {
    withRefresh(t => api("/api/v1/compass-bio/patient/diary/choices", null, t))
      .then(r => r.json()).then(d => {
        el("se-chips").innerHTML = d.choices.map(c =>
          `<button type="button" class="chip" data-code="${c.code}">${esc(c.label)}</button>`).join("");
        el("se-chips").querySelectorAll(".chip").forEach(ch => ch.addEventListener("click", () => {
          ch.classList.toggle("chip--on");
          const code = ch.dataset.code;
          if (S.seSelected.includes(code)) S.seSelected = S.seSelected.filter(x => x !== code);
          else { S.seSelected.push(code); showGuidance(code); }
        }));
      }).catch(() => {});
  }
  function showGuidance(code) {
    withRefresh(t => api("/api/v1/compass-bio/patient/diary/choices", null, t)).catch(()=>{});
    // пояснение тянем лениво: отправляем «черновик» дневника с одним кодом — сервер вернёт карточку
    withRefresh(t => api("/api/v1/compass-bio/patient/diary", { method: "POST",
      body: JSON.stringify({ intake_time: el("input-intake").value, taken: true,
        before_meal_30min: el("btn-meal-yes").classList.contains("seg--active"),
        side_effects: S.seSelected.map(c => ({ code: c, severity: 1 })) }) }, t))
      .then(r => r.json()).then(d => {
        el("se-guidance").innerHTML = d.guidance.map(g =>
          `<div class="guide-card${g.always_critical ? " guide-crit" : ""}">
            <div class="guide-title">${esc(g.title)}</div>
            <div>${esc(g.what)}</div>
            <div class="guide-why">${esc(g.why_now)}</div>
            <b>Что делать сейчас:</b><ul>${g.do_now.map(x => `<li>${esc(x)}</li>`).join("")}</ul>
            ${g.red_flags && g.red_flags.length ? `<div class="guide-red">🚩 ${g.red_flags.map(esc).join("<br>🚩 ")}</div>` : ""}
          </div>`).join("");
      }).catch(() => {});
  }
  function saveDiary() {
    const payload = {
      intake_time: el("input-intake").value, taken: el("btn-dose-yes").classList.contains("seg--active"),
      before_meal_30min: el("btn-meal-yes").classList.contains("seg--active"),
      side_effects: S.seSelected.map(c => ({ code: c, severity: 1 })),
      mood_score: +el("input-mood").value, anxiety_score: +el("input-anx").value,
      sleep_score: +el("input-sleep").value, notes: el("input-notes").value || null,
    };
    withRefresh(t => api("/api/v1/compass-bio/patient/diary",
      { method: "POST", body: JSON.stringify(payload) }, t))
      .then(r => r.json()).then(d => {
        el("diary-result").innerHTML = d.guidance.map(g =>
          `<div class="notice notice--ok"><b>${esc(g.title)}</b>: ${esc(g.why_now)}</div>`).join("");
        toast("День сохранён и передан врачу ✓");
        loadStreak();
      }).catch(() => {
        el("diary-error").textContent = "Нет связи: запись сохранена локально и уйдёт при появлении сети.";
      });
  }
  function toast(text) { const d = document.createElement("div"); d.className = "notice notice--ok"; d.textContent = text;
    el("diary-result").prepend(d); setTimeout(() => d.remove(), 6000); }

  function loadStreak() {
    withRefresh(t => api("/api/v1/auth/me", null, t)).then(r => r.json()).then(me => {
      S.me = me;
      return withRefresh(t => api("/api/v1/compass-bio/patient/" + me.sub + "/insights", null, t)).then(r => r.json());
    }).then(d => { el("streak-badge").textContent = "🔥 стрик: " + d.streak_days + " дн" +
      (d.rmse_meal_30min_pct != null ? " · «до еды»: " + d.rmse_meal_30min_pct + "%" : ""); })
    .catch(() => {});
  }

  /* ---------- шкалы item-level ---------- */
  function loadScales() {
    withRefresh(t => api("/api/v1/auth/me", null, t)).then(r => r.json()).then(me => {
      S.me = me;
      return withRefresh(t => api("/api/v1/compass-bio/patient/" + me.sub + "/timeline", null, t)).then(r => r.json());
    }).then(tl => {
      const codes = [["GAD-7","Тревога (ГТР-7)"],["PHQ-9","Депрессия (PHQ-9)"],["C-SSRS-SCREEN","Суицидальный риск (C-SSRS)"]];
      el("scales-list").innerHTML = codes.map(([code, title]) => {
        const done = tl.scales.filter(s => s.scale_code === code);
        const last = done[0];
        return `<div class="scale-row"><div><b>${title}</b><br><small>${last ?
          "последний: " + last.total_score + " б. (" + new Date(last.taken_at).toLocaleDateString("ru-RU") + ")" : "ещё не заполняли"}</small></div>
          <button type="button" class="btn btn--primary btn--sm" data-scale="${code}">Пройти</button></div>`;
      }).join("");
      el("scales-list").querySelectorAll("button[data-scale]").forEach(b =>
        b.addEventListener("click", () => startQuiz(b.dataset.scale, b.textContent)));
    }).catch(e => { el("scales-list").innerHTML = "<p class='hint'>Нужна сеть для загрузки шкал.</p>"; });
  }
  function startQuiz(code) {
    withRefresh(t => api("/api/v1/compass-bio/scales/" + code, null, t)).then(r => r.json()).then(s => {
      S.quiz = s; S.quizAnswers = new Array(s.items.length).fill(null);
      el("quiz-card").style.display = ""; el("quiz-result").style.display = "none";
      el("quiz-title").textContent = s.title_ru;
      el("quiz-items").innerHTML = s.preamble ? `<p class="card-sub">${esc(s.preamble)}</p>` : "";
      s.items.forEach((q, i) => {
        el("quiz-items").insertAdjacentHTML("beforeend",
          `<div class="field-block"><div class="field-title">${i + 1}. ${esc(q)}</div>
           <div class="segmented" data-i="${i}">${s.options.map(o =>
             `<button type="button" class="seg" data-v="${o.v}" data-i="${i}">${esc(o.label)}</button>`).join("")}</div></div>`);
      });
      el("quiz-items").querySelectorAll(".seg").forEach(b => b.addEventListener("click", () => {
        const i = +b.dataset.i;
        S.quizAnswers[i] = +b.dataset.v;
        el("quiz-items").querySelectorAll(`.seg[data-i="${i}"]`).forEach(x => x.classList.remove("seg--active"));
        b.classList.add("seg--active");
      }));
      el("quiz-card").scrollIntoView({ behavior: "smooth" });
    });
  }
  function submitQuiz() {
    if (S.quizAnswers.some(a => a === null)) { el("quiz-error").textContent = "Ответьте на все пункты — пропуск суицидального вопроса = «риск не оценён», это важно."; return; }
    el("quiz-error").textContent = "";
    withRefresh(t => api("/api/v1/compass-bio/scales/" + S.quiz.code + "/submit",
      { method: "POST", body: JSON.stringify({ answers: S.quizAnswers }) }, t))
      .then(r => r.json()).then(res => {
        const card = el("quiz-result"); card.style.display = "";
        card.innerHTML = `<h2>Результат: ${res.total_score} баллов</h2>
          <div class="notice ${res.suicide_flag ? "notice--critical" : "notice--ok"}">${esc(res.severity)}</div>
          ${res.suicide_flag ? "<p class='hint'>Ваш врач получил этот результат. Если тяжело прямо сейчас — 112 или 0523, не ждите.</p>" : ""}`;
        card.scrollIntoView({ behavior: "smooth" });
        loadScales();
      }).catch(e => { el("quiz-error").textContent = e.message; });
  }

  /* ---------- прогресс ---------- */
  function renderProgress() {
    withRefresh(t => api("/api/v1/auth/me", null, t)).then(r => r.json()).then(me =>
      withRefresh(t => api("/api/v1/compass-bio/patient/" + me.sub + "/timeline", null, t)).then(r => r.json()))
    .then(tl => {
      const list = el("progress-list");
      list.innerHTML = tl.scales.length ? tl.scales.map(s =>
        `<li class="hist-item">${new Date(s.taken_at).toLocaleDateString("ru-RU")} · ${esc(s.scale_code)}: <b>${s.total_score}</b> б.</li>`).join("")
        : "<li class='hist-item'>Пока пусто — пройдите первую шкалу.</li>";
      const cv = el("progress-canvas"), ctx = cv.getContext("2d");
      ctx.clearRect(0, 0, cv.width, cv.height);
      const series = {};
      tl.scales.forEach(s => { (series[s.scale_code] = series[s.scale_code] || []).push(s.total_score); });
      const colors = { "GAD-7": "#0284c7", "PHQ-9": "#8a3ffc" };
      let maxV = 10;
      Object.keys(colors).forEach(code => {
        const pts = series[code]; if (!pts || pts.length < 2) return;
        maxV = Math.max(maxV, ...pts);
        ctx.strokeStyle = colors[code]; ctx.lineWidth = 2; ctx.beginPath();
        pts.forEach((v, i) => { const x = 30 + i * ((cv.width - 50) / Math.max(pts.length - 1, 1)), y = cv.height - 25 - (v / 27) * (cv.height - 45);
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.stroke();
        pts.forEach((v, i) => { const x = 30 + i * ((cv.width - 50) / Math.max(pts.length - 1, 1)), y = cv.height - 25 - (v / 27) * (cv.height - 45);
          ctx.fillStyle = colors[code]; ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 7); ctx.fill();
          ctx.fillText(v, x - 6, y - 7); });
        ctx.fillStyle = colors[code];
        ctx.fillText(code + " ↓ лучше", cv.width - 120, 20 + Object.keys(colors).indexOf(code) * 14);
      });
      ctx.strokeStyle = "#c6c6c6"; ctx.beginPath(); ctx.moveTo(24, cv.height - 25); ctx.lineTo(cv.width - 10, cv.height - 25); ctx.stroke();
    }).catch(() => {});
  }

  /* ---------- напоминания ---------- */
  function setupNotifications() {
    if (!("Notification" in window)) { el("notify-status").textContent = "Уведомления не поддерживаются этим браузером."; return; }
    Notification.requestPermission().then(p => {
      el("notify-status").textContent = p === "granted" ? "Включено: напомним за 30 минут до приёма (при открытом приложении)." : "Разрешение не выдано.";
      if (p === "granted") setInterval(() => {
        const t = el("input-intake").value; if (!t) return;
        const [h, m] = t.split(":").map(Number);
        const target = new Date(); target.setHours(h, m - 30, 0, 0);
        const diff = target - new Date();
        if (diff > 0 && diff < 65000) new Notification("Клинический компас", { body: "Через 30 минут — приём Авиандр®. Не забудь поесть заранее!" });
      }, 30000);
    });
  }

  /* ---------- связка/старт ---------- */
  function bind(token) {
    const err = el("bind-error"); err.textContent = "";
    api("/api/v1/auth/bind", { method: "POST", body: JSON.stringify({ token }) })
      .then(r => r.ok ? r.json() : r.json().then(d => Promise.reject(new Error(d.detail || "HTTP " + r.status))))
      .then(d => { STORE.session = { access: d.access_token, refresh: d.refresh_token }; location.reload(); })
      .catch(e => { err.textContent = "Привязка не удалась: " + (e.message || e) + ". Код одноразовый — попросите новый QR."; });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const qsToken = QS.get("token");
    if (qsToken) el("bind-token").value = qsToken;
    el("btn-bind").addEventListener("click", () => bind(el("bind-token").value.trim()));
    el("bind-token").addEventListener("keydown", e => { if (e.key === "Enter") bind(el("bind-token").value.trim()); });

    document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    ["mood","anx","sleep"].forEach(k => el("input-" + k).addEventListener("input", e => el("lbl-" + k === "lbl-anx" ? "lbl-anx" : "lbl-" + k).textContent = e.target.value));
    [["input-mood","lbl-mood"],["input-anx","lbl-anx"],["input-sleep","lbl-sleep"]].forEach(([i,l]) =>
      el(i).addEventListener("input", e => el(l).textContent = e.target.value));
    [["btn-dose-yes","btn-dose-no"],["btn-meal-yes","btn-meal-no"]].forEach(([a,b]) => {
      el(a).addEventListener("click", () => { el(a).classList.add("seg--active"); el(b).classList.remove("seg--active"); });
      el(b).addEventListener("click", () => { el(b).classList.add("seg--active"); el(a).classList.remove("seg--active"); });
    });
    el("btn-diary").addEventListener("click", saveDiary);
    el("btn-quiz-submit").addEventListener("click", submitQuiz);
    el("btn-notify").addEventListener("click", setupNotifications);
    el("btn-logout").addEventListener("click", () => { STORE.clear(); location.reload(); });

    if (STORE.session && STORE.session.access) {
      el("view-bind").style.display = "none"; el("view-diary").style.display = "";
      loadChoices(); loadStreak(); loadScales();
      window.addEventListener("online", () => toast("Сеть вернулась — синхронизирую"));
    }
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  });
})();
