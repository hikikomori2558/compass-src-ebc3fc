/*
 * frontend_doctor/clinical_engines_client.js
 * ===========================================
 * Единый клиентский слой АРМ врача (восстановлен после потери из git):
 *
 *   CompassAuth            — логин врача (JWT Bearer), refresh, fetchWithAuth, QR-приглашение
 *   CompassAlerts          — WebSocket-шина критических алертов + подтверждение человеком
 *   CompassClinicalEngines — движки для app.js: Bitap-NER ЭМК, PopPK 168 ч и 4 canvas-рендера
 *
 * Источник значений — FastAPI/C11-бэкенд (POST /api/v1/cdss/evaluate-encounter:
 * RK45 Дормана—Принса, LPC-4/TKEO + Демпстер—Шефер, точные значения Оуэна).
 * Если сервер недоступен или врач не залогинен, включается ЛОКАЛЬНАЯ офлайн-аппроксимация —
 * она честно помечена (state.source = 'local') и не притворяется C11.
 *
 * Контракт для app.js (синхронный, как было до потери файла):
 *   CompassClinicalEngines.scanClinicalText(text)  -> { elapsedMs, matches[], source }
 *   CompassClinicalEngines.solvePopPK168h(params)  -> { cMax, cTrough, auc, halfLife, curve[], source }
 *   CompassClinicalEngines.requestEvaluation(ctx)  -> асинхронно: дергает onServerResponse
 *   CompassClinicalEngines.onServerResponse = fn   — app.js подписывается и делает recompute
 *   renderTrajectory36wCanvas / renderPKCanvas / renderDSTMassesCanvas / renderOwenCanvas
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- конфиг
  const QS = new URLSearchParams(window.location.search);
  // Стенд обычно живёт на виртуалке/сервере в локальной сети, а смотрят с хоста
  // или телефона: если страницу открыли НЕ с localhost, API ищем на том же хосте
  // (http://<этот-hostname>:8000), а не в 127.0.0.1 браузера. Ручной ввод в форме
  // входа или ?api=... всегда приоритетнее.
  const DEFAULT_API = (function () {
    const h = window.location.hostname;
    if (h && h !== "127.0.0.1" && h !== "localhost" && h !== "0.0.0.0") {
      return "http://" + h + ":8000";
    }
    return window.location.protocol === "https:" ? "" : "http://127.0.0.1:8000";
  })();
  const CFG = {
    apiBase: (QS.get("api") || localStorage.getItem("compass_api_base") || DEFAULT_API).replace(/\/+$/, ""),
    debounceMs: 350,
    wsRetryMs: 5000
  };

  const Auth = {
    access: null, refresh: null, sub: null, role: null, login: null, isDoctor: false,

    restore() {
      try {
        const s = JSON.parse(localStorage.getItem("compass_doctor_session") || "null");
        if (s && s.access) Object.assign(this, s);
      } catch (e) { /* повреждённая сессия = просто нет сессии */ }
      return !!this.access;
    },

    save() {
      localStorage.setItem("compass_doctor_session", JSON.stringify({
        access: this.access, refresh: this.refresh, sub: this.sub,
        role: this.role, login: this.login, isDoctor: this.isDoctor
      }));
    },

    clear() {
      this.access = this.refresh = this.sub = this.role = this.login = null;
      this.isDoctor = false;
      localStorage.removeItem("compass_doctor_session");
    },

    async signIn(login, password) {
      const r = await fetch(CFG.apiBase + "/api/v1/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password })
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || ("HTTP " + r.status));
      }
      const d = await r.json();
      this.access = d.access_token; this.refresh = d.refresh_token;
      this.sub = d.principal_sub; this.role = d.principal_role;
      this.login = login; this.isDoctor = String(d.principal_role).startsWith("DOCTOR");
      this.save();
      return d;
    },

    async doRefresh() {
      if (!this.refresh) return false;
      const r = await fetch(CFG.apiBase + "/api/v1/auth/refresh", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: this.refresh })
      });
      if (!r.ok) { this.clear(); return false; }
      const d = await r.json();
      this.access = d.access_token; this.refresh = d.refresh_token;
      this.sub = d.principal_sub; this.role = d.principal_role;
      this.save();
      return true;
    },

    /** fetch с Bearer; при 401 — одна попытка refresh + повтор. */
    async fetchWithAuth(path, opts) {
      const call = () => fetch(CFG.apiBase + path, Object.assign({}, opts, {
        headers: Object.assign({ "Authorization": "Bearer " + this.access }, opts && opts.headers || {})
      }));
      let r = await call();
      if (r.status === 401 && (await this.doRefresh())) r = await call();
      return r;
    },

    /** Одноразовое QR-приглашение пациента (TTL 24 ч, только ссылка без клиники). */
    async createInvite() {
      const r = await this.fetchWithAuth("/api/v1/auth/invite", { method: "POST" });
      if (!r.ok) throw new Error("Не удалось создать приглашение (HTTP " + r.status + ")");
      return await r.json(); // { invite_url, token, qr_png_base64, expires_at }
    }
  };

  const Alerts = {
    ws: null, timer: null, backoff: CFG.wsRetryMs,

    connect(onHello, onPing, onState) {
      this.disconnect();
      if (!Auth.access || !Auth.sub) return;
      const url = CFG.apiBase.replace(/^http/, "ws") + "/ws/alerts/" + encodeURIComponent(Auth.sub) +
        "?token=" + encodeURIComponent(Auth.access);
      try { this.ws = new WebSocket(url); } catch (e) { onState && onState("error"); return; }

      this.ws.onopen = () => { this.backoff = CFG.wsRetryMs; onState && onState("open"); };
      this.ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          if (m.kind === "HELLO") onHello && onHello(m);
          if (m.kind === "CRITICAL_PING") onPing && onPing(m);
        } catch (e) { /* мусор в канале игнорируем */ }
      };
      this.ws.onclose = () => {
        onState && onState("closed");
        // авто-реконнект с постоянным интервалом: канал алертов не должен молча умирать
        this.timer = setTimeout(() => this.connect(onHello, onPing, onState), this.backoff);
      };
      this.ws.onerror = () => { try { this.ws.close(); } catch (e) { /* уже закрыт */ } };
    },

    disconnect() {
      clearTimeout(this.timer);
      if (this.ws) { try { this.ws.close(); } catch (e) { /* noop */ } this.ws = null; }
    },

    async unconfirmed() {
      const r = await Auth.fetchWithAuth("/api/v1/alerts/unconfirmed");
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    },

    async confirm(alertId, actionTaken) {
      const r = await Auth.fetchWithAuth("/api/v1/alerts/" + alertId + "/confirm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_taken: actionTaken })
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json(); // { seconds_to_human_confirmation } — метрика Условий 5/6
    }
  };

  // ================================================================ ДВИЖКИ
  const Engines = {
    state: { source: "local", serverLatencyMs: null, note: "локальная офлайн-аппроксимация" },
    onServerResponse: null,

    _pending: null, _timer: null,
    _byNote: new Map(),   // noteText -> { ts, scan }
    _byPk: new Map(),     // значение PK-параметров (JSON-ключ) -> { ts, pk }

    /** Ключ по ЗНАЧЕНИЮ параметров: app.js создаёт объект заново на каждый пересчет,
     *  поэтому кэш по ссылке объекта никогда бы не попал. */
    pkKey(p) {
      return JSON.stringify([p.doseMg, p.intervalH, p.cyp2d6AS, p.cyp2d6Inh, p.cyp2d6Mbi, p.cyp3a4Inh, p.egfr]);
    },

    markLocal() { this.state.source = "local"; this.state.serverLatencyMs = null; },

    /** Асинхронный прогон полного контекста визита на C11-бэкенде (с дебаунсом). */
    requestEvaluation(ctx) {
      if (!Auth.access) { this.markLocal(); return; }
      clearTimeout(this.timer);
      this._pending = ctx;
      this.timer = setTimeout(() => this._evaluateNow(this._pending), CFG.debounceMs);
    },

    async _evaluateNow(ctx) {
      const payload = {
        patient_id: ctx.patientId || "RU-77-4092",
        doctor_specialty: ctx.role || "psychiatrist",
        ehr_note_text: ctx.noteText || "",
        hama_score: ctx.hama ?? 6,
        madrs_score: ctx.madrs ?? 7,
        // PHQ-9/GAD-7 в интерфейсе не вводятся: пересчёт из HAM-A/MADRS только как вход
        // микросетей (визуальная серьёзность), не как клиническая оценка.
        phq9_score: Math.round((ctx.madrs ?? 7) * 0.6),
        gad7_score: Math.round((ctx.hama ?? 6) * 0.7),
        phq9_item9_suicidal: ctx.suicidal ?? 0,
        self_report_wellbeing: ctx.wellbeing ?? 0.9,
        cyp2d6_activity_score: ctx.pkParams ? ctx.pkParams.cyp2d6AS : 0.12,
        egfr_ml_min: ctx.pkParams ? ctx.pkParams.egfr : 78,
        aviandr_dose_mg: ctx.pkParams ? ctx.pkParams.doseMg : 20,
        aviandr_interval_h: ctx.pkParams ? ctx.pkParams.intervalH : 12,
        active_comedications: ctx.activeComedicationNames || [],
        raw_kintsugi_dep_score: ctx.kintsugiDep ?? 0.84,
        raw_kintsugi_anx_score: ctx.kintsugiAnx ?? 0.87,
        response_latency_sec: 4.2
      };
      try {
        const t0 = performance.now();
        const r = await Auth.fetchWithAuth("/api/v1/cdss/evaluate-encounter", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const resp = await r.json();
        const ms = performance.now() - t0;

        this._byNote.set(ctx.noteText, { ts: Date.now(), scan: this._mapServerScan(ctx.noteText, resp) });
        if (ctx.pkParams) this._byPk.set(Engines.pkKey(ctx.pkParams), { ts: Date.now(), pk: this._mapServerPK(resp), full: resp });

        this.state.source = "server";
        this.state.serverLatencyMs = ms;
        this.state.note = "C11-ядро (RK45 + Bitap + DST + микросети W8A8)";
        this.lastServerFull = { ctx: ctx, resp: resp, ts: Date.now() };
        if (this.onServerResponse) this.onServerResponse(resp);
      } catch (e) {
        // сервер недоступен/401 после refresh — тихо живём на локальной аппроксимации
        this.markLocal();
        this.state.note = "бэкенд недоступен: " + (e.message || e);
      }
    },

    _mapServerScan(noteText, resp) {
      // utf8_byte_span из C -> символьные индексы JS-строки для подсветки в textarea
      const byteMap = buildUtf8ByteMap(noteText);
      const CAT_LABEL = { 1: "DRUG", 2: "DRUG", 3: "DDI", 4: "DDI", 5: "SCALE", 6: "ICD" };
      const matches = (resp.extracted_entities || []).map(function (e) {
        let start = -1, end = -1;
        const span = e.utf8_byte_span || [];
        if (span.length === 2 && byteMap[span[0]] !== undefined && byteMap[span[1]] !== undefined) {
          start = byteMap[span[0]]; end = byteMap[span[1]];
        }
        const surface = e.matched_surface_text || "";
        if (start < 0) { // страховка: ищем вхождение без учёта регистра
          const idx = noteText.toLowerCase().indexOf(surface.toLowerCase());
          if (idx >= 0) { start = idx; end = idx + surface.length; }
        }
        let impact;
        if (e.category_code === 2) impact = "АБСОЛЮТНОЕ ПРОТИВОПОКАЗАНИЕ ОХЛП";
        else if (e.category_code === 3) impact = "Ингибитор CYP2D6 (+" + (e.ddi_ki_ratio_impact || 0).toFixed(1) + ")";
        else if (e.category_code === 4) impact = "Ингибитор CYP3A4 (+" + (e.ddi_ki_ratio_impact || 0).toFixed(1) + ")";
        else impact = "Сущность ЭМК распознана";
        return {
          start: start, end: end, surface: surface, canon: e.canonical_name || surface,
          cat: CAT_LABEL[e.category_code] || "EHR", dist: e.edit_distance || 0,
          isNegated: !!e.is_negated, impact: impact
        };
      }).filter(function (m) { return m.start >= 0; });
      return { elapsedMs: resp.latency_ms || 0, matches: matches, source: "server" };
    },

    _mapServerPK(resp) {
      const p = resp.poppk_rk45 || {};
      return {
        cMax: p.c_max_ng_ml || 0, cTrough: p.c_trough_ss_ng_ml || 0,
        auc: p.auc_0_168_ng_h_ml || 0, halfLife: p.effective_half_life_h || 0,
        curve: p.conc_plasma_168h_ng_ml || [], risk: p.risk_classification || "",
        doseFactor: p.recommended_dose_adjustment_factor || 1.0,
        source: "server"
      };
    },

    // -------------------------------------------------- публичный контракт
    scanClinicalText(text) {
      const cached = this._byNote.get(text);
      if (cached && Date.now() - cached.ts < 60000) return cached.scan;
      return localScan(text);
    },

    solvePopPK168h(params) {
      const cached = this._byPk.get(Engines.pkKey(params));
      if (cached && Date.now() - cached.ts < 60000) return cached.pk;
      return localPopPK(params);
    },

    lastServerFull: null
  };

  // обратная карта «байтовое смещение UTF-8 -> индекс символа» для span-ов C-лексера
  function buildUtf8ByteMap(s) {
    const inv = { 0: 0 };   // boundary в байтах -> сколько символов ему предшествует
    let bytes = 0;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c >= 0xD800 && c <= 0xDBFF) { bytes += 0; }        // старший суррогат: пара считается на младшем
      else if (c >= 0xDC00 && c <= 0xDFFF) { bytes += 4; }   // младший суррогат: 4 байта пары
      else if (c < 0x80) bytes += 1;
      else if (c < 0x800) bytes += 2;
      else bytes += 3;
      if (inv[bytes] === undefined) inv[bytes] = i + 1;
    }
    return inv;
  }

  // ------------------------------------------------ локальный Bitap-подобный NER (офлайн-фоллбэк)
  // Словарь — компактное зеркало клинического словаря c_core/bitap_ru_lexer.c.
  // Точный нечёткий поиск (k<=2 опечатки, «Авеандр», «кларитромецин») даёт сервер;
  // здесь — упрощённый точный поиск + расстояние Левенштейна на отдельных словах.
  const LOCAL_DICT = [
    { w: "авиандр", canon: "Авиандр® (маритупирдин)", cat: "DRUG", impact: "Препарат терапии", ki: 0 },
    { w: "маритупирдин", canon: "Авиандр® (маритупирдин)", cat: "DRUG", impact: "Препарат терапии", ki: 0 },
    { w: "транилципромин", canon: "Транилципромин (ИМАО)", cat: "DRUG", impact: "АБСОЛЮТНОЕ ПРОТИВОПОКАЗАНИЕ ОХЛП", ki: 0, hard: true },
    { w: "транилципрамин", canon: "Транилципромин (ИМАО)", cat: "DRUG", impact: "АБСОЛЮТНОЕ ПРОТИВОПОКАЗАНИЕ ОХЛП", ki: 0, hard: true },
    { w: "фенелзин", canon: "Фенелзин (ИМАО)", cat: "DRUG", impact: "АБСОЛЮТНОЕ ПРОТИВОПОКАЗАНИЕ ОХЛП", ki: 0, hard: true },
    { w: "имао", canon: "Ингибиторы МАО (класс)", cat: "DRUG", impact: "АБСОЛЮТНОЕ ПРОТИВОПОКАЗАНИЕ ОХЛП", ki: 0, hard: true },
    { w: "пароксетин", canon: "Пароксетин", cat: "DDI", impact: "Ингибитор CYP2D6 (+4.8)", ki: 4.8 },
    { w: "парокситин", canon: "Пароксетин", cat: "DDI", impact: "Ингибитор CYP2D6 (+4.8)", ki: 4.8 },
    { w: "флуоксетин", canon: "Флуоксетин", cat: "DDI", impact: "Ингибитор CYP2D6 (+3.6)", ki: 3.6 },
    { w: "кларитромицин", canon: "Кларитромицин", cat: "DDI", impact: "Ингибитор CYP3A4 (+3.8)", ki: 3.8 },
    { w: "кларитромецин", canon: "Кларитромицин", cat: "DDI", impact: "Ингибитор CYP3A4 (+3.8)", ki: 3.8 },
    { w: "флуконазол", canon: "Флуконазол", cat: "DDI", impact: "Ингибитор CYP3A4 (+2.9)", ki: 2.9 },
    { w: "метопролол", canon: "Метопролол", cat: "DDI", impact: "Верифицированно безопасно (РКИ)", ki: 0 },
    { w: "дулоксетин", canon: "Дулоксетин", cat: "DDI", impact: "Верифицированно безопасно (РКИ)", ki: 0 },
    { w: "ципаофлоксацин", canon: "Ципрофлоксацин", cat: "DDI", impact: "Ингибитор CYP1A2 (умеренно)", ki: 0 },
    { w: "ципрофлоксацин", canon: "Ципрофлоксацин", cat: "DDI", impact: "Ингибитор CYP1A2 (умеренно)", ki: 0 },
    { w: "f41.1", canon: "МКБ-10 F41.1 (ГТР)", cat: "ICD", impact: "Показание ОХЛП", ki: 0 },
    { w: "f43.2", canon: "МКБ-10 F43.2 (расстройство адаптации)", cat: "ICD", impact: "Показание ОХЛП", ki: 0 },
    { w: "f32.1", canon: "МКБ-10 F32.1 (депрессивный эпизод)", cat: "ICD", impact: "Вторая линия", ki: 0 },
    { w: "gad-7", canon: "Шкала GAD-7", cat: "SCALE", impact: "Психометрия", ki: 0 },
    { w: "гтр-7", canon: "Шкала GAD-7 (ГТР-7, рус.)", cat: "SCALE", impact: "Психометрия", ki: 0 },
    { w: "phq-9", canon: "Шкала PHQ-9", cat: "SCALE", impact: "Психометрия", ki: 0 },
    { w: "ham-a", canon: "Шкала HAM-A", cat: "SCALE", impact: "Психометрия", ki: 0 },
    { w: "madrs", canon: "Шкала MADRS", cat: "SCALE", impact: "Психометрия", ki: 0 },
    { w: "c-ssrs", canon: "Шкала C-SSRS", cat: "SCALE", impact: "Суицидальный риск", ki: 0 },
    { w: "cyp2d6", canon: "CYP2D6 (фармакогенетика)", cat: "ICD", impact: "Метаболизм", ki: 0 }
  ];
  const NEGATION_CUES = ["отрицает", "не принимает", "не получал", "не получала", "отрицает.",
    "категорически отрицает", "не употребляет", "без приема", "не пьет", "не пьёт"];

  function levenshtein(a, b, maxK) {
    if (Math.abs(a.length - b.length) > maxK) return maxK + 1;
    const prev = new Array(b.length + 1);
    const cur = new Array(b.length + 1);
    for (let j = 0; j <= b.length; j++) prev[j] = j;
    for (let i = 1; i <= a.length; i++) {
      cur[0] = i;
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      for (let j = 0; j <= b.length; j++) prev[j] = cur[j];
    }
    return prev[b.length];
  }

  function localScan(text) {
    const t0 = performance.now();
    const matches = [];
    const lower = text.toLowerCase();
    // токен начинается с буквы, но может содержать цифры/точки/дефисы (f41.1, гтр-7, c-ssrs);
    // чистые числа (дозы «20 мг») в токены не попадают
    const re = /[a-zа-яё][a-zа-яё0-9.-]*/g;
    // Окно отрицания обрезается по границам предложения: до сущности берём хвост после
    // последнего [.!?], после сущности — голову до первого [.!?]. Иначе «отрицает.
    // Шкалы: ГТР-7» ложно заносит ГТР-7 в отрицания, а «…транилципрамина … отрицает.»
    // наоборот теряет признак.
    const tailAfterLastStop = function (s) {
      const cut = Math.max(s.lastIndexOf("."), s.lastIndexOf("!"), s.lastIndexOf("?"));
      return cut >= 0 ? s.slice(cut + 1) : s;
    };
    const headBeforeFirstStop = function (s) {
      const cut = Math.min.apply(null, [s.indexOf("."), s.indexOf("!"), s.indexOf("?")].filter(function (i) { return i >= 0; }));
      return cut >= 0 ? s.slice(0, cut) : s;
    };
    let m;
    while ((m = re.exec(lower)) !== null) {
      const word = m[0].replace(/[.-]+$/, "");
      if (!word) continue;
      let best = null, bestDist = 99;
      for (let i = 0; i < LOCAL_DICT.length; i++) {
        const d = word === LOCAL_DICT[i].w ? 0 : levenshtein(word, LOCAL_DICT[i].w, 2);
        if (d <= 2 && d < bestDist) { best = LOCAL_DICT[i]; bestDist = d; }
      }
      if (!best) continue;
      // Отрицание может стоять и ДО, и ПОСЛЕ сущности («отрицает приём ИМАО» /
      // «приём транилципрамина категорически отрицает») — окна в обе стороны.
      const before = tailAfterLastStop(lower.slice(Math.max(0, m.index - 48), m.index));
      const after = headBeforeFirstStop(lower.slice(m.index + word.length, m.index + word.length + 40));
      const isNegated = NEGATION_CUES.some(function (cue) {
        return before.includes(cue) || after.includes(cue);
      });
      matches.push({
        start: m.index, end: m.index + word.length, surface: text.slice(m.index, m.index + word.length),
        canon: best.canon, cat: best.cat, dist: bestDist, isNegated: isNegated,
        impact: isNegated ? "Отрицается пациентом" : best.impact
      });
    }
    matches.sort(function (a, b) { return a.start - b.start; });
    return { elapsedMs: performance.now() - t0, matches: matches, source: "local" };
  }

  // ------------------------------------------------ локальная PopPK-аппроксимация (офлайн-фоллбэк)
  // Однокомпартментная модель с кинетикой первого порядка и суперпозицией доз.
  // Это НЕ C11 RK45 Дормана—Принса: без Михаэлиса—Ментен и двух компартментов.
  // Назначение — не дать демо умереть без сервера; калибрована в окно ОХЛП 65–240.
  function localPopPK(p) {
    const t0 = performance.now();
    const dose = p.doseMg || 20, interval = p.intervalH || 12;
    const AS = Math.min(Math.max(p.cyp2d6AS == null ? 1 : p.cyp2d6AS, 0.05), 2.5);
    const mbi = p.cyp2d6Mbi == null ? 1 : p.cyp2d6Mbi;
    const ddi = 1 + 0.42 * ((p.cyp2d6Inh || 0) + 0.7 * (p.cyp3a4Inh || 0));
    const asFactor = 1 + 0.85 * (1 - Math.min(AS * mbi, 1));
    const egfr = Math.min(Math.max(p.egfr || 90, 15), 160);
    const renalFactor = Math.pow(Math.min(Math.max(90 / egfr, 0.8), 1.6), 0.35);
    const mult = Math.min(4.6, ddi * asFactor * renalFactor);

    const t12 = 15.0 * Math.sqrt(mult);          // ч, базовый T1/2 маритупирдина ~15 ч
    const ke = Math.LN2 / t12, ka = 1.15;         // 1/ч
    const nDoses = Math.floor(168 / interval);
    const targetCmax = 88.0 * mult;               // референс (AS=1, без DDI) -> Cmax ~ 88 нг/мл

    // Суперпозиция разовых доз формы (e^-ke*t - e^-ka*t); кривую нормируем численно
    // по заданному целевому Cmax — так модель всегда положительна и попадает в окно.
    const shape = function (dt) { return Math.exp(-ke * dt) - Math.exp(-ka * dt); };
    const raw = [];
    let rawMax = 0;
    for (let h = 1; h <= 168; h++) {
      let c = 0;
      for (let d = 0; d < nDoses; d++) {
        const td = d * interval;
        if (h - 1 < td) break;
        c += shape(h - td) * (dose / 20.0);
      }
      raw.push(c);
      if (c > rawMax) rawMax = c;
    }
    const scale = rawMax > 0 ? targetCmax / rawMax : 0;
    const curve = raw.map(function (c) { return c * scale; });

    let cMax = 0, auc = 0;
    curve.forEach(function (c) { auc += c; if (c > cMax) cMax = c; });
    const cTrough = curve[167] || 0;
    return {
      cMax: cMax, cTrough: cTrough, auc: auc, halfLife: t12, curve: curve,
      elapsedMs: performance.now() - t0, source: "local"
    };
  }

  // ================================================================ CANVAS-РЕНДЕРЫ
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || v; }
  function prepCanvas(canvas) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = "10px 'IBM Plex Mono', monospace";
    return ctx;
  }

  function renderPKCanvas(canvas, pkSol, pkRef) {
    const ctx = prepCanvas(canvas);
    const W = canvas.width, H = canvas.height, padL = 34, padB = 18, padT = 8;
    const data = (pkSol && pkSol.curve && pkSol.curve.length) ? pkSol.curve : [];
    const ref = (pkRef && pkRef.curve && pkRef.curve.length) ? pkRef.curve : [];
    const all = data.concat(ref, [320]);
    const maxC = Math.max.apply(null, all.concat([350]));
    const x = function (h) { return padL + (h / 168) * (W - padL - 6); };
    const y = function (c) { return H - padB - (c / maxC) * (H - padB - padT); };

    // терапевтическое окно ОХЛП 65–240 и порог безопасности 320
    ctx.fillStyle = "rgba(25,128,56,0.07)";
    ctx.fillRect(padL, y(240), W - padL - 6, y(65) - y(240));
    ctx.strokeStyle = "#da1e28"; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y(320)); ctx.lineTo(W - 6, y(320)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#da1e28"; ctx.fillText("320 (порог безопасности)", padL + 4, y(320) - 3);

    if (ref.length) {
      ctx.strokeStyle = "#8d8d8d"; ctx.lineWidth = 1.2; ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ref.forEach(function (c, i) { i ? ctx.lineTo(x(i + 1), y(c)) : ctx.moveTo(x(1), y(c)); });
      ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = "#8d8d8d"; ctx.fillText("референс: AS=1.0, без DDI", W - 190, y(ref[ref.length - 1]) - 5);
    }
    if (data.length) {
      ctx.strokeStyle = pkSol.cMax >= 320 ? "#da1e28" : "#0f62fe"; ctx.lineWidth = 2;
      ctx.beginPath();
      data.forEach(function (c, i) { i ? ctx.lineTo(x(i + 1), y(c)) : ctx.moveTo(x(1), y(c)); });
      ctx.stroke();
      ctx.fillStyle = pkSol.cMax >= 320 ? "#da1e28" : "#0f62fe";
      ctx.fillText("Cmax = " + pkSol.cMax.toFixed(1) + " нг/мл" + (pkSol.source === "server" ? " (C11 RK45)" : " (локальная аппроксимация)"), padL + 4, padT + 8);
    }
    // ось времени
    ctx.strokeStyle = "#c6c6c6"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, H - padB); ctx.lineTo(W - 6, H - padB); ctx.stroke();
    ctx.fillStyle = "#525252";
    [0, 24, 48, 72, 96, 120, 144, 168].forEach(function (h) {
      ctx.fillText(h + "ч", x(h) - 6, H - 5);
    });
  }

  function renderTrajectory36wCanvas(canvas, opts) {
    const ctx = prepCanvas(canvas);
    const W = canvas.width, H = canvas.height, padL = 30, padB = 16, padT = 8;
    const pts = (opts && opts.points) || [];
    // коридор РКИ Сеченовского университета (SIGH-A 22.06 -> 6.90 за 36 нед.)
    const RKI = [[0, 22.06], [2, 19.12], [8, 14.12], [16, 9.84], [24, 7.44], [36, 6.90]];
    const maxW = 36, maxY = 32;
    const x = function (w) { return padL + (w / maxW) * (W - padL - 6); };
    const y = function (v) { return H - padB - (v / maxY) * (H - padB - padT); };

    ctx.fillStyle = "rgba(2,130,199,0.10)";
    ctx.beginPath();
    RKI.forEach(function (p, i) { i ? ctx.lineTo(x(p[0]), y(p[1] * 1.18)) : ctx.moveTo(x(p[0]), y(p[1] * 1.18)); });
    for (let i = RKI.length - 1; i >= 0; i--) ctx.lineTo(x(RKI[i][0]), y(RKI[i][1] * 0.82));
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "#0284c7"; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
    ctx.beginPath();
    RKI.forEach(function (p, i) { i ? ctx.lineTo(x(p[0]), y(p[1])) : ctx.moveTo(x(p[0]), y(p[1])); });
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#0284c7"; ctx.fillText("коридор РКИ (Сеченовский ун-т, N=98)", padL + 4, padT + 8);

    function line(key, color, label) {
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
      let started = false;
      pts.forEach(function (p) {
        if (p[key] == null) return;
        started ? ctx.lineTo(x(p.week), y(p[key])) : ctx.moveTo(x(p.week), y(p[key]));
        started = true;
      });
      ctx.stroke();
      if (pts.length) {
        const last = pts[pts.length - 1];
        ctx.fillStyle = color;
        ctx.fillText(label + " = " + (last[key] != null ? last[key] : "?"), x(last.week) - 70, y(last[key] || 0) - 6);
      }
    }
    line("hama", "#0f62fe", "HAM-A");
    line("bioEquiv", "#8a3ffc", "Био-индекс");

    pts.forEach(function (p) {
      if (p.missedDoses) {
        ctx.fillStyle = "#da1e28";
        ctx.beginPath(); ctx.arc(x(p.week), y(p.hama || 0) + 10, 3.2, 0, Math.PI * 2); ctx.fill();
      }
    });
    ctx.strokeStyle = "#c6c6c6"; ctx.beginPath(); ctx.moveTo(padL, H - padB); ctx.lineTo(W - 6, H - padB); ctx.stroke();
    ctx.fillStyle = "#525252";
    [0, 6, 12, 18, 24, 30, 36].forEach(function (w) { ctx.fillText(w + "нд", x(w) - 8, H - 4); });
  }

  function renderDSTMassesCanvas(canvas, masses) {
    const ctx = prepCanvas(canvas);
    const W = canvas.width, H = canvas.height;
    const groups = ["ЭМК / самоотчёт", "Голосовой DAM", "Слияние Д-Ш"];
    const hyp = ["Ремиссия", "Тревога", "Депрессия", "Неопр."];
    const colors = ["#198038", "#0f62fe", "#8a3ffc", "#8d8d8d"];
    const bw = 18, groupW = (W - 20) / 3;
    const maxM = 1.0;
    hyp.forEach(function (h, hi) {
      ctx.fillStyle = "#525252";
      ctx.fillText(h.length > 7 ? h.slice(0, 7) + "." : h, 10 + hi * 0 + (groupW - bw * 4) / 2 + hi * (bw + 2) - 4, 10);
    });
    groups.forEach(function (g, gi) {
      const arr = masses[["ehr", "dam", "fused"][gi]] || [];
      ctx.fillStyle = "#161616";
      ctx.fillText(g, 10 + gi * groupW + 4, H - 4);
      arr.forEach(function (v, hi) {
        const bh = Math.min(v, maxM) * (H - 44);
        const bx = 10 + gi * groupW + 8 + hi * (bw + 2);
        ctx.fillStyle = colors[hi];
        ctx.fillRect(bx, H - 20 - bh, bw, bh);
      });
    });
    ctx.strokeStyle = "#c6c6c6"; ctx.beginPath(); ctx.moveTo(8, H - 20); ctx.lineTo(W - 8, H - 20); ctx.stroke();
  }

  function renderOwenCanvas(canvas, unions) {
    const ctx = prepCanvas(canvas);
    const W = canvas.width, H = canvas.height;
    const list = (unions || []).slice().sort(function (a, b) { return b.val - a.val; }).slice(0, 6);
    const maxV = Math.max.apply(null, list.map(function (u) { return u.val; }).concat([1]));
    list.forEach(function (u, i) {
      const y = 8 + i * 22;
      ctx.fillStyle = "#161616";
      ctx.fillText(u.name.length > 22 ? u.name.slice(0, 21) + "…" : u.name, 4, y + 9);
      const bx = 150, bw = Math.max(2, (u.val / maxV) * (W - bx - 44));
      ctx.fillStyle = u.val > maxV * 0.55 ? "#da1e28" : (u.val > maxV * 0.3 ? "#b28600" : "#0f62fe");
      ctx.fillRect(bx, y, bw, 12);
      ctx.fillStyle = "#525252";
      ctx.fillText(u.val.toFixed(1), bx + bw + 4, y + 10);
    });
  }

  // ================================================================ экспорт
  window.CompassAPIBase = CFG.apiBase;
  window.CompassAuth = Auth;
  window.CompassAlerts = Alerts;
  window.CompassClinicalEngines = {
    scanClinicalText: function (t) { return Engines.scanClinicalText(t); },
    solvePopPK168h: function (p) { return Engines.solvePopPK168h(p); },
    requestEvaluation: function (ctx) { Engines.requestEvaluation(ctx); },
    set onServerResponse(fn) { Engines.onServerResponse = fn; },
    get onServerResponse() { return Engines.onServerResponse; },
    get state() { return Engines.state; },
    /** Последний полный ответ /evaluate-encounter + контекст запроса (null, если не было). */
    get serverFull() { return Engines.lastServerFull; },
    renderPKCanvas: renderPKCanvas,
    renderTrajectory36wCanvas: renderTrajectory36wCanvas,
    renderDSTMassesCanvas: renderDSTMassesCanvas,
    renderOwenCanvas: renderOwenCanvas
  };
})();
