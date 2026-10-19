/*
 * frontend_doctor/app.js
 * ======================
 * Контроллер медицинской карты врача (OpenMRS O3 + Medplum + HL7 CDS Hooks):
 * - Реальное интерактивное управление листом назначений (CPOE), шкалами (HAM-A, MADRS, GAD-7, PHQ-9)
 *   и текстом осмотра с автоматическим вызовом C11/FastAPI бэкенда (и локальным фоллбэком).
 * - Строгое визуальное разделение маршрутизации к психиатру: «ОБЯЗАТЕЛЬНО» vs «ЖЕЛАТЕЛЬНО».
 * - Принцип «Врач в контуре»: двухфазное подписание клинических решений.
 */

(function () {
  "use strict";

  const PATIENTS_DB = {
    pt_sokolova: {
      id: "RU-77-4092",
      avatar: "ЕС",
      fullName: "Соколова Елена Викторовна",
      sexAge: "Женский • 38 лет (14.05.1988)",
      ehrId: "ИБ № 4092/26",
      oms: "ОМС: 7700 4910 8821 0492",
      chips: [
        { text: "МКБ-10: F41.1 ГТР + F32.1", alert: false },
        { text: "CYP2D6 *4/*4 (Медленный метаболизатор, AS=0.12)", alert: true },
        { text: "СКФ 78 мл/мин • Child-Pugh A", alert: false },
        { text: "ЭКГ: QTc 468 мс", alert: true }
      ],
      drugTitle: "Авиандр® 20 мг × 2 р/сут",
      drugTarget: "Маритупирдин • 5-HT7 / 5-HT2A / α2 (Неделя 6)",
      wearableVal: "Сон 4.1 ч | RMSSD 16 мс",
      wearableSub: "Часы пациента: 4 пробуждения за ночь, симпатикотония",
      adherencePct: 64,
      adherenceNote: "Пропущено 5 приёмов «Авиандр®» 20 мг за последние 7 суток",
      doseCalendar14d: [
        { day: "06.10", am: "ok", pm: "ok" },
        { day: "07.10", am: "ok", pm: "ok" },
        { day: "08.10", am: "ok", pm: "warn" },
        { day: "09.10", am: "ok", pm: "ok" },
        { day: "10.10", am: "ok", pm: "ok" },
        { day: "11.10", am: "warn", pm: "ok" },
        { day: "12.10", am: "ok", pm: "miss" },
        { day: "13.10", am: "ok", pm: "ok" },
        { day: "14.10", am: "miss", pm: "ok" },
        { day: "15.10", am: "ok", pm: "miss" },
        { day: "16.10", am: "ok", pm: "ok" },
        { day: "17.10", am: "miss", pm: "warn" },
        { day: "18.10", am: "ok", pm: "miss" },
        { day: "19.10", am: "ok", pm: "ok" }
      ],
      cpoeMeds: [
        { id: "m_aviandr", active: true, locked: true, name: "Авиандр® (Маритупирдин, таб. 20 мг, ГК ХимРар)", regimen: "20 мг × 2 р/сут", target: "Антагонист 5-HT7 / 5-HT2A / α2 • Субстрат CYP2D6/3A4", ddiNote: "Базовый препарат", ddi2d6: 0, ddi3a4: 0 },
        { id: "m_parox", active: true, locked: false, name: "Пароксетин (таб. 20 мг)", regimen: "20 мг × 1 р/сут (утром)", target: "СИОЗС • Сильный ингибитор CYP2D6 (MBI)", ddiNote: "КРИТИЧЕСКОЕ DDI: Блокада метаболизма Авиандра (Ki=4.8)", ddi2d6: 4.8, ddi3a4: 0 },
        { id: "m_clari", active: true, locked: false, name: "Кларитромицин (таб. 500 мг)", regimen: "500 мг × 2 р/сут (курс 7 дн.)", target: "Макролид • Сильный ингибитор CYP3A4", ddiNote: "ОПАСНОЕ DDI: Блокада резервного пути CYP3A4 (Ki=3.8)", ddi2d6: 0, ddi3a4: 3.8 },
        { id: "m_maoi", active: false, locked: false, name: "Транилципромин (ИМАО, 10 мг)", regimen: "Отменен (>14 дней назад)", target: "Неселективный ИМАО", ddiNote: "АБСОЛЮТНОЕ ПРОТИВОПОКАЗАНИЕ ОХЛП", ddi2d6: 0, ddi3a4: 0, isContra: true }
      ],
      scalesState: { hama: 6, madrs: 7, wellbeingPct: 92, suicidalItem9: 0 },
      cyp2d6AS: 0.12,
      egfr: 78.0,
      templates: {
        structured:
          "Пациентка принимает Авеандр 20 мг дважды в сутки в течение 6 недель. На приеме утверждает, что чувствует себя полностью здоровой и настаивает на немедленной отмене терапии. " +
          "Сопутствующая терапия: самостоятельно продолжила прием парокситина 20 мг утром и принимает кларитромецин 500 мг по поводу острого синусита. " +
          "Приём транилципрамина и фенилзина категорически отрицает. Отмечается мелкий тремор пальцев рук, признаков акатезии нет.",
        epicrisis:
          "ВЫПИСНОЙ ЭПИКРИЗ (неструктурированный шаблон): б-ная 38 л., д-з F41.1 / F32.1. Назначен Авиандр 20мг х 2р/д. " +
          "Из анамнеза: параллельно пьет парокситин 20мг и антибиотик кларитромецин. ИМАО (транилципрамин) отрицает. " +
          "По данным голосового дневника между визитами — выраженные паузы хезитации до 950 мс, хотя по анкете пишет что жалоб нет.",
        primary_care:
          "ПРИЕМ ВРАЧА-ТЕРАПЕВТА: Обратилась за продлением рецепта. Со слов пациентки — состояние отличное, тревоги нет, просит отменить Авеандр. " +
          "Одновременно принимает парокситин и кларитромецин. Признаков брадикардии нет, ЭКГ QTc 468 мс."
      },
      bioMetrics: [
        { name: "Нестабильность глоттального остатка TKEO (LPC-4)", val: "0.54 у.е.", norm: "0.18 ± 0.07", badge: "Тремор +4.8σ", crit: true },
        { name: "Параметр пауз хезитации Вейбулла k (без смычек)", val: "k = 0.56 (λ = 950 мс)", norm: "k = 1.25 ± 0.28", badge: "Психомоторный блок", crit: true },
        { name: "Пертурбация Jitter PPQ5 / Shimmer APQ5", val: "1.72% / 5.60%", norm: "< 0.65% / < 3.0%", badge: "Дисфония тревоги", crit: true },
        { name: "W2-калиброванный скор речи (ИК-1 RU)", val: "Dep: 0.84 | Anx: 0.87", norm: "< 0.35", badge: "Скрытый рецидив", crit: true }
      ],
      bioSeverityEquiv: 26,
      trajPoints: [
        { week: 0, hama: 27, bioEquiv: 27, missedDoses: false },
        { week: 2, hama: 21, bioEquiv: 20, missedDoses: false },
        { week: 4, hama: 16, bioEquiv: 18, missedDoses: false },
        { week: 6, hama: 6,  bioEquiv: 26, missedDoses: true }
      ],
      timeline: [
        { time: "Сегодня, 08:14", type: "ГОЛОСОВОЙ ДНЕВНИК (25 СЕК)", text: "При отправке утреннего голосового отчета выявлен высокий тремор голосовых связок (TKEO 0.54) и паузы хезитации 950 мс при анкете «жалоб нет».", alert: true },
        { time: "Вчера, 22:30", type: "ПРОПУСК ДОЗЫ ПРЕПАРАТА", text: "Пациентка не подтвердила вечерний приём Авиандр® 20 мг (5-й пропуск за 7 дней). Комментарий в дневнике: «выраженная сонливость днём».", alert: true },
        { time: "17.10, 07:45", type: "ДАННЫЕ НОСИМОГО УСТРОЙСТВА", text: "Продолжительность сна снизилась до 4 ч 05 мин, ночная вариабельность сердечного ритма RMSSD = 16 мс.", alert: false }
      ],
      chat: [
        { from: "patient", time: "08:20", text: "Доктор, здравствуйте! Мне уже совсем хорошо, тревога прошла. Можно я со следующей недели перестану принимать Авиандр?" },
        { from: "doctor", time: "08:42", text: "Елена Викторовна, здравствуйте. Вижу ваши данные в карте. Пожалуйста, не прекращайте приём самостоятельно — сегодня на приёме скорректируем схему сопутствующих препаратов, чтобы убрать сонливость." }
      ]
    },

    pt_morozov: {
      id: "RU-77-5108",
      avatar: "ИМ",
      fullName: "Морозов Игорь Андреевич",
      sexAge: "Мужской • 44 года (03.11.1981)",
      ehrId: "ИБ № 5108/26",
      oms: "ОМС: 7700 6120 3319 8841",
      chips: [
        { text: "МКБ-10: F43.2 Расстройство адаптации + U09.9 Пост-COVID", alert: false },
        { text: "CYP2D6 *1/*2 (Нормальный метаболизатор, AS=1.5)", alert: false },
        { text: "СКФ 94 мл/мин • АЛТ/АСТ норма", alert: false }
      ],
      drugTitle: "Старт: Авиандр® 20 мг × 2 р/сут",
      drugTarget: "Маритупирдин • 5-HT7 / 5-HT2A (Неделя 2)",
      wearableVal: "Сон 6.2 ч | RMSSD 34 мс",
      wearableSub: "Умеренное нарушение засыпания, положительная динамика",
      adherencePct: 93,
      adherenceNote: "Принято 26 из 28 доз за 14 дней (высокая приверженность)",
      doseCalendar14d: Array.from({ length: 14 }, (_, i) => ({
        day: (i + 6 < 10 ? "0" : "") + (i + 6) + ".10",
        am: "ok",
        pm: i === 5 ? "warn" : (i === 11 ? "miss" : "ok")
      })),
      cpoeMeds: [
        { id: "m_aviandr", active: true, locked: true, name: "Авиандр® (Маритупирдин, таб. 20 мг, ГК ХимРар)", regimen: "20 мг × 2 р/сут", target: "Антагонист 5-HT7 / 5-HT2A • Показание: ГТР, СР, пост-COVID", ddiNote: "Терапевтический оптимум", ddi2d6: 0, ddi3a4: 0 },
        { id: "m_parox", active: false, locked: false, name: "Пароксетин (таб. 20 мг)", regimen: "Не назначен", target: "СИОЗС • Сильный ингибитор CYP2D6", ddiNote: "Нет взаимодействия (отключен)", ddi2d6: 4.8, ddi3a4: 0 }
      ],
      scalesState: { hama: 21, madrs: 16, wellbeingPct: 48, suicidalItem9: 0 },
      cyp2d6AS: 1.5,
      egfr: 94.0,
      templates: {
        structured:
          "Первичное обращение к врачу-терапевту / неврологу после перенесенного COVID-19. Жалобы на постоянное внутреннее напряжение, трудности засыпания, лабильность АД. " +
          "Назначен препарат Авиандр 20 мг 2 раза в сутки. Приём транилципромина, фенелзина, флуоксетина и пароксетина отрицает.",
        epicrisis: "Амбулаторное наблюдение F43.2 (постковидный тревожный синдром). На фоне старта Авиандр 20 мг х 2 р/сут — хорошая переносимость.",
        primary_care: "Осмотр терапевта поликлиники: тревожно-вегетативная симптоматика (HAM-A 21 б., GAD-7 14 б.). Абсолютных противопоказаний нет."
      },
      bioMetrics: [
        { name: "Нестабильность глоттального остатка TKEO (LPC-4)", val: "0.26 у.е.", norm: "0.18 ± 0.07", badge: "Умеренная тревога", crit: false },
        { name: "Параметр пауз хезитации Вейбулла k", val: "k = 1.14 (λ = 370 мс)", norm: "k = 1.25 ± 0.28", badge: "Согласовано со шкалами", crit: false }
      ],
      bioSeverityEquiv: 20,
      trajPoints: [
        { week: 0, hama: 26, bioEquiv: 25, missedDoses: false },
        { week: 2, hama: 21, bioEquiv: 20, missedDoses: false }
      ],
      timeline: [
        { time: "Сегодня, 09:00", type: "УТРЕННИЙ САМООТЧЕТ", text: "Пациент заполнил опросник GAD-7 и подтвердил утренний приём Авиандр® 20 мг. Нежелательных явлений не отмечает.", alert: false }
      ],
      chat: [
        { from: "patient", time: "09:05", text: "Доброе утро! Таблетки переношу хорошо, сон стал немного глубже." }
      ]
    },

    pt_kravtsova: {
      id: "RU-77-6319",
      avatar: "АК",
      fullName: "Кравцова Анна Сергеевна",
      sexAge: "Женский • 31 год (22.08.1995)",
      ehrId: "ИБ № 6319/26",
      oms: "ОМС: 7700 9012 4410 1120",
      chips: [
        { text: "МКБ-10: F41.1 ГТР (Стойкая ремиссия, Нед. 12)", alert: false },
        { text: "CYP2D6 *1/*1 (Нормальный метаболизатор, AS=1.4)", alert: false },
        { text: "Приверженность 98%", alert: false }
      ],
      drugTitle: "Авиандр® 20 мг × 2 р/сут",
      drugTarget: "Маритупирдин • Поддерживающий курс (Неделя 12)",
      wearableVal: "Сон 7.6 ч | RMSSD 49 мс",
      wearableSub: "Физиологический профиль сна и парасимпатического тонуса",
      adherencePct: 98,
      adherenceNote: "Принято 28 из 28 доз за последние 14 дней",
      doseCalendar14d: Array.from({ length: 14 }, (_, i) => ({
        day: (i + 6 < 10 ? "0" : "") + (i + 6) + ".10",
        am: "ok",
        pm: "ok"
      })),
      cpoeMeds: [
        { id: "m_aviandr", active: true, locked: true, name: "Авиандр® (Маритупирдин, таб. 20 мг, ГК ХимРар)", regimen: "20 мг × 2 р/сут", target: "Антагонист 5-HT7 / 5-HT2A", ddiNote: "Оптимальная экспозиция", ddi2d6: 0, ddi3a4: 0 }
      ],
      scalesState: { hama: 5, madrs: 4, wellbeingPct: 90, suicidalItem9: 0 },
      cyp2d6AS: 1.4,
      egfr: 102.0,
      templates: {
        structured: "Контрольный осмотр на 12-й неделе терапии препаратом Авиандр 20 мг 2 раза в сутки. Стойкая клиническая ремиссия, тревоги нет. Сопутствующих ингибиторов не принимает, пароксетин и флуоксетин отрицает.",
        epicrisis: "Этапный эпикриз: 12 недель терапии Авиандр 20 мг х 2 р/сут, достигнута полная ремиссия ГТР (HAM-A 5 баллов).",
        primary_care: "Плановый визит: жалоб нет, полная комплаентность."
      },
      bioMetrics: [
        { name: "Нестабильность глоттального остатка TKEO (LPC-4)", val: "0.17 у.е.", norm: "0.18 ± 0.07", badge: "Норма", crit: false },
        { name: "Параметр пауз хезитации Вейбулла k", val: "k = 1.28 (λ = 295 мс)", norm: "k = 1.25 ± 0.28", badge: "Норма", crit: false }
      ],
      bioSeverityEquiv: 5,
      trajPoints: [
        { week: 0, hama: 25, bioEquiv: 25, missedDoses: false },
        { week: 2, hama: 19, bioEquiv: 19, missedDoses: false },
        { week: 4, hama: 14, bioEquiv: 14, missedDoses: false },
        { week: 8, hama: 8,  bioEquiv: 8,  missedDoses: false },
        { week: 12, hama: 5, bioEquiv: 5,  missedDoses: false }
      ],
      timeline: [
        { time: "Сегодня, 08:00", type: "КОНТРОЛЬ РЕМИССИИ", text: "Пациентка отметила 14-й день подряд без единого пропуска дозы. HAM-A = 5 б.", alert: false }
      ],
      chat: [
        { from: "patient", time: "08:15", text: "Здравствуйте! Самочувствие стабильное, продолжаю приём по схеме." }
      ]
    }
  };

  let activePtKey = "pt_sokolova";

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* Полный пересчет клинической картины (При изменении текста ЭМК, препаратов в CPOE или ползунков шкал) */
  function recalculateClinicalState() {
    const pt = PATIENTS_DB[activePtKey];
    const role = document.getElementById("doctor-role-select").value;
    const noteText = document.getElementById("soap-note-textarea").value;

    // 1. Сканирование текста ЭМК лексером Bitap
    const scanRes = window.CompassClinicalEngines.scanClinicalText(noteText);
    document.getElementById("top-latency-val").textContent = `${(scanRes.elapsedMs + 0.28).toFixed(2)} мс`;

    // Инлайн-подсветка в SOAP
    let html = "";
    let cur = 0;
    const sorted = [...scanRes.matches].sort((a, b) => a.start - b.start);
    sorted.forEach((m) => {
      if (m.start < cur) return;
      html += escapeHtml(noteText.slice(cur, m.start));
      const cls = m.isNegated ? "ner-negated" : (m.cat === "DRUG" ? "ner-drug" : "ner-ddi");
      const tag = m.isNegated ? "ИСКЛЮЧЕНО (ОТРИЦАНИЕ)" : `${m.cat} d=${m.dist}`;
      html += `<span class="ner-mark ${cls}">${escapeHtml(m.surface)} [${tag}]</span>`;
      cur = m.end;
    });
    html += escapeHtml(noteText.slice(cur));
    document.getElementById("soap-annotated-preview").innerHTML = html;

    // Таблица распознанных сущностей
    const entTbody = document.getElementById("soap-entities-tbody");
    entTbody.innerHTML = "";
    scanRes.matches.forEach((m) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(m.canon)}</strong></td>
        <td class="mono">"${escapeHtml(m.surface)}"</td>
        <td class="mono">d = ${m.dist}</td>
        <td>${m.isNegated ? '<span class="cds--tag cds--tag--cool-gray">Отрицается пациентом</span>' : `<span class="cds--tag cds--tag--red">${escapeHtml(m.impact)}</span>`}</td>
      `;
      entTbody.appendChild(tr);
    });

    // 2. Сбор активных ко-медикаментов из интерактивной таблицы CPOE + текста
    const regimenVal = document.getElementById("cpoe-aviandr-regimen").value;
    let doseMg = 20.0, intervalH = 12.0;
    if (regimenVal === "20_24") { doseMg = 20.0; intervalH = 24.0; }
    else if (regimenVal === "10_12") { doseMg = 10.0; intervalH = 12.0; }

    let ddi2d6 = 0.0, ddi3a4 = 0.0, hasMAOI = false;
    pt.cpoeMeds.forEach((med) => {
      if (med.active) {
        ddi2d6 += med.ddi2d6 || 0;
        ddi3a4 += med.ddi3a4 || 0;
        if (med.isContra) hasMAOI = true;
      }
    });

    // Решаем систему ОДУ RK45 на 168 часов
    const pkSol = window.CompassClinicalEngines.solvePopPK168h({
      doseMg: doseMg,
      intervalH: intervalH,
      cyp2d6AS: pt.cyp2d6AS,
      cyp2d6Inh: ddi2d6,
      cyp2d6Mbi: ddi2d6 > 2.0 ? 0.35 : 1.0,
      cyp3a4Inh: ddi3a4,
      egfr: pt.egfr
    });

    const pkRef = window.CompassClinicalEngines.solvePopPK168h({
      doseMg: 20.0,
      intervalH: 12.0,
      cyp2d6AS: 1.0,
      cyp2d6Inh: 0.0,
      cyp2d6Mbi: 1.0,
      cyp3a4Inh: 0.0,
      egfr: 90.0
    });

    // Обновляем KPI фармакокинетики на Сводке и в CPOE
    document.getElementById("sum-cmax-val").textContent = `${pkSol.cMax.toFixed(1)} нг/мл`;
    document.getElementById("sum-cmax-val").style.color = pkSol.cMax >= 320 ? "#da1e28" : (pkSol.cMax > 240 ? "#b28600" : "#198038");
    document.getElementById("cpoe-cmax").textContent = `${pkSol.cMax.toFixed(1)} нг/мл`;
    document.getElementById("cpoe-cmax").style.color = pkSol.cMax >= 320 ? "#da1e28" : "#198038";
    document.getElementById("cpoe-ctrough").textContent = `${pkSol.cTrough.toFixed(1)} нг/мл`;
    document.getElementById("cpoe-auc").textContent = `${pkSol.auc.toFixed(0)} нг·ч/мл`;
    document.getElementById("cpoe-t12").textContent = `${pkSol.halfLife.toFixed(1)} ч`;
    const recFactor = Math.min(1.0, 160.0 / Math.max(pkSol.cMax, 1.0));
    document.getElementById("cpoe-dose-factor").textContent =
      pkSol.cMax > 250 ? `Снизить до ${(recFactor * 100).toFixed(0)}%` : "Коррекция не треб.";

    // 3. Пересчет психометрии и индекса диссимуляции Демпстера-Шефера
    const hama = pt.scalesState.hama;
    const madrs = pt.scalesState.madrs;
    const wellbeing = pt.scalesState.wellbeingPct / 100.0;
    const suicidal = pt.scalesState.suicidalItem9;

    document.getElementById("sum-scales-val").textContent = `HAM-A: ${hama} б. | MADRS: ${madrs} б.`;

    // Оценка конфликта Демпстера K между самоотчетом и объективным био-индексом
    const bioPathologyProb = Math.min(0.92, pt.bioSeverityEquiv / 30.0);
    const ehrRemissionProb = wellbeing * Math.max(0.05, 1.0 - (hama + madrs) / 65.0);
    const conflictK = Math.min(0.95, Math.abs(bioPathologyProb - (1.0 - ehrRemissionProb)) * 0.95);
    const dissimIdx = Math.min(0.95, ehrRemissionProb * bioPathologyProb * (1.0 + 0.5 * conflictK));

    document.getElementById("scales-dst-k").textContent = conflictK.toFixed(2);
    document.getElementById("scales-dst-dissim").textContent = dissimIdx.toFixed(2);
    document.getElementById("sum-scales-sub").textContent =
      dissimIdx >= 0.35
        ? `Диссонанс с голосовым маркером (K = ${conflictK.toFixed(2)}, диссимуляция!)`
        : `Согласовано с голосовым био-контуром (K = ${conflictK.toFixed(2)})`;

    // Обновляем последнюю точку на 36-недельной траектории
    if (pt.trajPoints.length > 0) {
      pt.trajPoints[pt.trajPoints.length - 1].hama = hama;
    }

    // 4. Формирование сигнала маршрутизации к психиатру ("ОБЯЗАТЕЛЬНО" vs "ЖЕЛАТЕЛЬНО")
    const isMandatory = (
      suicidal >= 1 ||
      hasMAOI ||
      pkSol.cMax >= 320.0 ||
      dissimIdx >= 0.35 ||
      madrs >= 30
    );
    const isRecommended = !isMandatory && (hama >= 16 || madrs >= 18 || pkSol.cMax > 240.0);

    const bannerBox = document.getElementById("banner-routing-widget");
    const badgeEl = document.getElementById("routing-category-badge");
    const descEl = document.getElementById("routing-category-desc");

    if (isMandatory) {
      bannerBox.className = "banner-routing-widget";
      badgeEl.textContent = "КАТЕГОРИЯ: ОБЯЗАТЕЛЬНО";
      const reasons = [];
      if (suicidal >= 1) reasons.push(`суицидальный риск по п.9 PHQ-9 (${suicidal} б.)`);
      if (hasMAOI) reasons.push("абсолютное противопоказание ОХЛП (совместный приём с ИМАО)");
      if (pkSol.cMax >= 320.0) reasons.push(`опасная кумуляция Авиандра (Cmax ${pkSol.cMax.toFixed(0)} нг/мл из-за DDI CYP2D6/3A4)`);
      if (dissimIdx >= 0.35) reasons.push(`маскированная диссимуляция тяжелой тревоги (индекс ${dissimIdx.toFixed(2)})`);
      if (madrs >= 30) reasons.push(`тяжелый депрессивный эпизод (MADRS ${madrs} б.)`);
      descEl.textContent =
        role === "psychiatrist"
          ? `Требуется ургентный пересмотр схемы врачом-психиатром: ${reasons.join("; ")}.`
          : `ДЛЯ ВРАЧА (${role === "therapist" ? "ТЕРАПЕВТА" : "НЕВРОЛОГА"}): Немедленное направление к врачу-психиатру (Форма 057/у-04) — ${reasons.join("; ")}.`;
    } else if (isRecommended) {
      bannerBox.className = "banner-routing-widget mode-recommended";
      badgeEl.textContent = "КАТЕГОРИЯ: ЖЕЛАТЕЛЬНО";
      descEl.textContent =
        "Показана плановая консультация врача-психиатра / психотерапевта для верификации динамики и подключения КПТ на фоне приёма Авиандр®.";
    } else {
      bannerBox.className = "banner-routing-widget mode-stable";
      badgeEl.textContent = "КАТЕГОРИЯ: ПЛАНОВОЕ ВЕДЕНИЕ";
      descEl.textContent =
        "Экстренных показаний к направлению к психиатру нет. Концентрация Авиандр® в терапевтическом окне [65–240 нг/мл], конкордантная динамика.";
    }

    // 5. Генерация карточек HL7 CDS Hooks и чек-листа подтверждения врачом
    renderCDSHooksAndStagedOrders(pt, pkSol, dissimIdx, conflictK, hasMAOI, suicidal, isMandatory, isRecommended);

    // 6. Отрисовка всех Canvas-графиков
    window.CompassClinicalEngines.renderTrajectory36wCanvas(
      document.getElementById("canvas-summary-traj"),
      { points: pt.trajPoints }
    );
    window.CompassClinicalEngines.renderPKCanvas(
      document.getElementById("canvas-summary-pk"),
      pkSol,
      pkRef
    );
    window.CompassClinicalEngines.renderPKCanvas(
      document.getElementById("canvas-cpoe-pk"),
      pkSol,
      pkRef
    );
    window.CompassClinicalEngines.renderDSTMassesCanvas(
      document.getElementById("canvas-scales-dst"),
      {
        ehr: [ehrRemissionProb, 1 - ehrRemissionProb - 0.1, 0.05, 0.05],
        dam: [1 - bioPathologyProb - 0.08, bioPathologyProb * 0.75, bioPathologyProb * 0.25, 0.08],
        fused: [Math.max(0.05, (ehrRemissionProb + (1 - bioPathologyProb)) * 0.4), bioPathologyProb * 0.65, 0.15, 0.10]
      }
    );

    const owenUnions = [
      { name: "CYP2D6/3A4 DDI", val: (ddi2d6 + ddi3a4) * 3.1 + (pt.cyp2d6AS < 0.3 ? 8.5 : 0.8) },
      { name: "Диссимуляция DST", val: dissimIdx * 28.0 },
      { name: "Голос (LPC-4/TKEO)", val: bioPathologyProb * 18.0 },
      { name: "Психометрия", val: (hama + madrs) * 0.45 + suicidal * 15.0 },
      { name: "Комплаенс 14 дн.", val: (100 - pt.adherencePct) * 0.25 },
      { name: "QTc / Органы", val: pkSol.cMax > 300 ? 11.4 : 1.2 }
    ].sort((a, b) => b.val - a.val);

    const totRisk = owenUnions.reduce((s, u) => s + u.val, 0);
    document.getElementById("xai-total-risk-label").textContent = `Индекс риска: ${totRisk.toFixed(1)}`;
    window.CompassClinicalEngines.renderOwenCanvas(
      document.getElementById("canvas-rail-owen"),
      owenUnions
    );
  }

  function renderCDSHooksAndStagedOrders(pt, pkSol, dissimIdx, conflictK, hasMAOI, suicidal, isMandatory, isRecommended) {
    const stream = document.getElementById("cds-hooks-stream");
    const stagedList = document.getElementById("staged-orders-list");
    stream.innerHTML = "";
    stagedList.innerHTML = "";

    const cards = [];
    const stagedOrders = [];

    if (isMandatory) {
      cards.push({
        ind: "ind-critical",
        badge: "ОБЯЗАТЕЛЬНО",
        badgeCls: "cds--tag--red",
        title: "Направление к врачу-психиатру (Категория: ОБЯЗАТЕЛЬНО)",
        detail: suicidal >= 1
          ? "Выявлены суицидальные тенденции по п. 9 шкалы PHQ-9. Показана немедленная консультация психиатра."
          : "Высокий риск осложнений фармакотерапии и/или маскированной диссимуляции рецидива.",
        source: "Источник: Задачи инструмента п. 8 • КР Минздрава РФ F32 / F41.1"
      });
      stagedOrders.push({
        id: "ord_psych_ref",
        text: "Сформировать направление к врачу-психиатру (Форма 057/у-04, категория: ОБЯЗАТЕЛЬНО)"
      });
    } else if (isRecommended) {
      cards.push({
        ind: "ind-warning",
        badge: "ЖЕЛАТЕЛЬНО",
        badgeCls: "cds--tag--warm-gray",
        title: "Плановая консультация психиатра / психотерапевта (Категория: ЖЕЛАТЕЛЬНО)",
        detail: "Умеренно выраженная тревожно-депрессивная симптоматика. Целесообразно подключение КПТ в дополнение к Авиандр®.",
        source: "Источник: Задачи инструмента п. 8 • КР Минздрава РФ F41.1 / F43.2"
      });
      stagedOrders.push({
        id: "ord_psych_opt",
        text: "Назначить плановую консультацию врача-психотерапевта (категория: ЖЕЛАТЕЛЬНО)"
      });
    }

    if (pkSol.cMax >= 320.0 || hasMAOI) {
      cards.push({
        ind: "ind-critical",
        badge: "БЕЗОПАСНОСТЬ ОХЛП",
        badgeCls: "cds--tag--red",
        title: hasMAOI
          ? "Абсолютное противопоказание: совместный приём с ИМАО"
          : `Риск передозировки Авиандр® (Cmax = ${pkSol.cMax.toFixed(0)} нг/мл > 320 нг/мл)`,
        detail: "Ингибирование CYP2D6 (пароксетин) и CYP3A4 (кларитромицин) у медленного метаболизатора блокирует клиренс Маритупирдина.",
        source: "Источник: ГРЛС РФ ОХЛП «Авиандр®» (таб. 20 мг) • Раздел «Взаимодействия»"
      });
      stagedOrders.push({
        id: "ord_cancel_ddi",
        text: "Отменить сопутствующий ингибитор CYP2D6/3A4 и перевести Авиандр® на режим 10 мг × 2 р/сут под контролем ЭКГ"
      });
    } else {
      cards.push({
        ind: "ind-info",
        badge: "ГРЛС ОХЛП",
        badgeCls: "cds--tag--green",
        title: `Экспозиция Авиандр® в терапевтическом окне (Cmax = ${pkSol.cMax.toFixed(0)} нг/мл)`,
        detail: "Режим дозирования соответствует зарегистрированной инструкции ГРЛС РФ и метаболическому фенотипу пациента.",
        source: "Источник: ГРЛС РФ ОХЛП «Авиандр®» (Маритупирдин 20 мг, ГК ХимРар)"
      });
      stagedOrders.push({
        id: "ord_continue_aviandr",
        text: "Подтвердить продолжение приёма препарата «Авиандр®» в текущей дозировке"
      });
    }

    if (dissimIdx >= 0.35) {
      cards.push({
        ind: "ind-critical",
        badge: "ДИССИМУЛЯЦИЯ",
        badgeCls: "cds--tag--red",
        title: `Маскированная тревога / диссимуляция (K = ${conflictK.toFixed(2)})`,
        detail: "Пациент декларирует полное благополучие для отмены препарата, однако акустический анализ речи фиксирует выраженный тремор и паузы хезитации.",
        source: "Источник: Эвиденциальный био-контур Демпстера-Шефера (LPC-4 / TKEO)"
      });
      stagedOrders.push({
        id: "ord_block_premature_stop",
        text: "Отклонить запрос на преждевременную отмену терапии и назначить очный контроль"
      });
    }

    if (pt.adherencePct < 80) {
      cards.push({
        ind: "ind-warning",
        badge: "КОМПЛАЕНС",
        badgeCls: "cds--tag--warm-gray",
        title: `Снижение приверженности между визитами (${pt.adherencePct}%)`,
        detail: "Зафиксированы пропуски вечерних доз в мобильном дневнике пациента.",
        source: "Источник: Модуль межвизитного сопровождения «Авинейро»"
      });
      stagedOrders.push({
        id: "ord_push_ adherence",
        text: "Активировать смарт-напоминания о приёме дозы в мобильном приложении пациента"
      });
    }

    document.getElementById("cds-cards-count").textContent = `${cards.length} сигнала`;

    cards.forEach((c) => {
      const div = document.createElement("div");
      div.className = `cds-hook-card ${c.ind}`;
      div.innerHTML = `
        <div class="hook-card-top">
          <span class="hook-card-title">${escapeHtml(c.title)}</span>
          <span class="cds--tag ${c.badgeCls}">${escapeHtml(c.badge)}</span>
        </div>
        <div class="hook-card-detail">${escapeHtml(c.detail)}</div>
        <div class="hook-card-source">${escapeHtml(c.source)}</div>
      `;
      stream.appendChild(div);
    });

    stagedOrders.forEach((o) => {
      const lbl = document.createElement("label");
      lbl.className = "staged-item";
      lbl.innerHTML = `
        <input type="checkbox" class="staged-order-cb" value="${o.id}" checked />
        <span>${escapeHtml(o.text)}</span>
      `;
      stagedList.appendChild(lbl);
    });
  }

  function loadPatientIntoChart(ptKey) {
    activePtKey = ptKey;
    const pt = PATIENTS_DB[ptKey];

    document.getElementById("pt-avatar").textContent = pt.avatar;
    document.getElementById("pt-full-name").textContent = pt.fullName;
    document.getElementById("pt-sex-age").textContent = pt.sexAge;
    document.getElementById("pt-ehr-id").textContent = pt.ehrId;
    document.getElementById("pt-oms").textContent = pt.oms;

    const chipsBox = document.getElementById("pt-clinical-tags");
    chipsBox.innerHTML = "";
    pt.chips.forEach((ch) => {
      const sp = document.createElement("span");
      sp.className = "clinical-chip" + (ch.alert ? " chip-alert" : "");
      sp.textContent = ch.text;
      chipsBox.appendChild(sp);
    });

    document.getElementById("sum-drug-title").textContent = pt.drugTitle;
    document.getElementById("sum-drug-target").textContent = pt.drugTarget;
    document.getElementById("sum-wearable-val").textContent = pt.wearableVal;
    document.getElementById("sum-wearable-sub").textContent = pt.wearableSub;

    document.getElementById("sidenav-adherence-pct").textContent = pt.adherencePct + "%";
    document.getElementById("sidenav-adherence-fill").style.width = pt.adherencePct + "%";
    document.getElementById("sidenav-adherence-fill").style.backgroundColor =
      pt.adherencePct < 75 ? "#da1e28" : "#198038";
    document.getElementById("sidenav-adherence-note").textContent = pt.adherenceNote;

    // Рендер 14-дневного календаря приёма препарата между визитами
    const calGrid = document.getElementById("dose-calendar-grid");
    calGrid.innerHTML = "";
    pt.doseCalendar14d.forEach((d) => {
      const col = document.createElement("div");
      col.className = "dose-day-col";
      const mapCls = (st) => st === "ok" ? "slot-ok" : (st === "warn" ? "slot-warn" : "slot-miss");
      const mapTxt = (st) => st === "ok" ? "20 мг" : (st === "warn" ? "Опозд." : "Пропуск");
      col.innerHTML = `
        <span class="dose-day-label">${d.day}</span>
        <div class="dose-slots">
          <div class="dose-slot ${mapCls(d.am)}" title="Утренний приём">${mapTxt(d.am)}</div>
          <div class="dose-slot ${mapCls(d.pm)}" title="Вечерний приём">${mapTxt(d.pm)}</div>
        </div>
      `;
      calGrid.appendChild(col);
    });

    // Текст SOAP
    const tpl = document.getElementById("soap-template-select").value;
    document.getElementById("soap-note-textarea").value = pt.templates[tpl] || pt.templates.structured;

    // Рендер интерактивной таблицы листа назначений (CPOE)
    renderCPOETable(pt);

    // Ползунки шкал
    document.getElementById("input-hama-range").value = pt.scalesState.hama;
    document.getElementById("lbl-hama-val").textContent = `${pt.scalesState.hama} / 56 баллов`;
    document.getElementById("input-madrs-range").value = pt.scalesState.madrs;
    document.getElementById("lbl-madrs-val").textContent = `${pt.scalesState.madrs} / 60 баллов`;
    document.getElementById("input-wellbeing-range").value = pt.scalesState.wellbeingPct;
    document.getElementById("lbl-gad-val").textContent = `Субъективное благополучие: ${pt.scalesState.wellbeingPct}%`;
    document.getElementById("input-suicidal-select").value = String(pt.scalesState.suicidalItem9);

    // Таблица голосовых биомаркеров
    const bioTbody = document.getElementById("scales-bio-tbody");
    bioTbody.innerHTML = "";
    pt.bioMetrics.forEach((b) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(b.name)}</td>
        <td class="mono"><strong>${escapeHtml(b.val)}</strong></td>
        <td class="mono">${escapeHtml(b.norm)}</td>
        <td><span class="cds--tag ${b.crit ? "cds--tag--red" : "cds--tag--green"}">${escapeHtml(b.badge)}</span></td>
      `;
      bioTbody.appendChild(tr);
    });

    // Таймлайн и чат
    const tlList = document.getElementById("intervisit-timeline-list");
    tlList.innerHTML = "";
    pt.timeline.forEach((ev) => {
      const card = document.createElement("div");
      card.className = "timeline-card" + (ev.alert ? " tl-alert" : "");
      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px;">
          <strong class="mono">${escapeHtml(ev.time)}</strong>
          <span class="cds--tag ${ev.alert ? "cds--tag--red" : "cds--tag--teal"}">${escapeHtml(ev.type)}</span>
        </div>
        <div>${escapeHtml(ev.text)}</div>
      `;
      tlList.appendChild(card);
    });

    renderChatMessages(pt);
    document.getElementById("signoff-audit-log").textContent =
      "Статус: Черновик сформирован СППВР. Ожидает подписи лечащего врача.";
    document.getElementById("signoff-audit-log").style.backgroundColor = "#ffffff";

    recalculateClinicalState();
  }

  function renderCPOETable(pt) {
    const tbody = document.getElementById("cpoe-meds-tbody");
    tbody.innerHTML = "";
    const activeCount = pt.cpoeMeds.filter((m) => m.active).length;
    document.getElementById("nav-cpoe-badge").textContent = String(activeCount);

    pt.cpoeMeds.forEach((med) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>
          <input type="checkbox" class="cpoe-toggle-cb" data-med-id="${med.id}" ${med.active ? "checked" : ""} ${med.locked ? "disabled" : ""} />
        </td>
        <td><strong>${escapeHtml(med.name)}</strong></td>
        <td class="mono">${escapeHtml(med.regimen)}</td>
        <td>${escapeHtml(med.target)}</td>
        <td>
          <span class="cds--tag ${med.active && (med.ddi2d6 > 0 || med.ddi3a4 > 0 || med.isContra) ? "cds--tag--red" : "cds--tag--cool-gray"}">
            ${escapeHtml(med.ddiNote)}
          </span>
        </td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll(".cpoe-toggle-cb").forEach((cb) => {
      cb.addEventListener("change", (e) => {
        const id = e.target.getAttribute("data-med-id");
        const found = pt.cpoeMeds.find((m) => m.id === id);
        if (found) {
          found.active = e.target.checked;
          renderCPOETable(pt);
          recalculateClinicalState();
        }
      });
    });
  }

  function renderChatMessages(pt) {
    const box = document.getElementById("doctor-patient-chat");
    box.innerHTML = "";
    pt.chat.forEach((m) => {
      const div = document.createElement("div");
      div.className = "chat-bubble " + (m.from === "doctor" ? "from-doctor" : "from-patient");
      div.innerHTML = `<strong>${m.from === "doctor" ? "Лечащий врач" : "Пациент"} (${m.time}):</strong> ${escapeHtml(m.text)}`;
      box.appendChild(div);
    });
    box.scrollTop = box.scrollHeight;
  }

  function bindEvents() {
    // Переключение разделов левого меню карты
    document.querySelectorAll(".sidenav-link").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".sidenav-link").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".chart-view").forEach((v) => v.classList.remove("active"));
        btn.classList.add("active");
        const viewId = btn.getAttribute("data-view");
        const target = document.getElementById(viewId);
        if (target) target.classList.add("active");
        setTimeout(recalculateClinicalState, 20);
      });
    });

    // Смена пациента или специальности врача
    document.getElementById("patient-queue-select").addEventListener("change", (e) => {
      loadPatientIntoChart(e.target.value);
    });
    document.getElementById("doctor-role-select").addEventListener("change", recalculateClinicalState);

    // Смена шаблона документа ЭМК и живое редактирование текста
    document.getElementById("soap-template-select").addEventListener("change", (e) => {
      const pt = PATIENTS_DB[activePtKey];
      document.getElementById("soap-note-textarea").value =
        pt.templates[e.target.value] || pt.templates.structured;
      recalculateClinicalState();
    });
    document.getElementById("soap-note-textarea").addEventListener("input", recalculateClinicalState);

    // Смена дозировки Авиандра в CPOE
    document.getElementById("cpoe-aviandr-regimen").addEventListener("change", recalculateClinicalState);

    // Ползунки шкал в разделе Психометрия
    document.getElementById("input-hama-range").addEventListener("input", (e) => {
      const v = parseInt(e.target.value, 10);
      PATIENTS_DB[activePtKey].scalesState.hama = v;
      document.getElementById("lbl-hama-val").textContent = `${v} / 56 баллов`;
      recalculateClinicalState();
    });

    document.getElementById("input-madrs-range").addEventListener("input", (e) => {
      const v = parseInt(e.target.value, 10);
      PATIENTS_DB[activePtKey].scalesState.madrs = v;
      document.getElementById("lbl-madrs-val").textContent = `${v} / 60 баллов`;
      recalculateClinicalState();
    });

    document.getElementById("input-wellbeing-range").addEventListener("input", (e) => {
      const v = parseInt(e.target.value, 10);
      PATIENTS_DB[activePtKey].scalesState.wellbeingPct = v;
      document.getElementById("lbl-gad-val").textContent = `Субъективное благополучие: ${v}%`;
      recalculateClinicalState();
    });

    document.getElementById("input-suicidal-select").addEventListener("change", (e) => {
      PATIENTS_DB[activePtKey].scalesState.suicidalItem9 = parseInt(e.target.value, 10);
      recalculateClinicalState();
    });

    // Подписание выбранных действий врачом (Doctor-in-the-Loop Gate)
    document.getElementById("btn-sign-orders").addEventListener("click", () => {
      const checked = Array.from(document.querySelectorAll(".staged-order-cb:checked"));
      const logEl = document.getElementById("signoff-audit-log");
      if (checked.length === 0) {
        logEl.textContent = "Внимание: не выбрано ни одного клинического действия для проведения в ЭМК.";
        logEl.style.backgroundColor = "#fff1f1";
        return;
      }
      const roleText = document.getElementById("doctor-role-select").selectedOptions[0].text;
      const timeStr = new Date().toLocaleTimeString("ru-RU");
      logEl.textContent =
        `[ЭП ВРАЧА ${timeStr}] Проведено в ЭМК (${roleText}): утверждено назначений/направлений — ${checked.length}. Синхронизировано с приложением пациента.`;
      logEl.style.backgroundColor = "#defbe6";
    });

    // Чат с пациентом и быстрые шаблоны
    document.querySelectorAll(".quick-tpl-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.getElementById("chat-text-input").value = btn.getAttribute("data-msg");
      });
    });

    document.getElementById("chat-send-button").addEventListener("click", () => {
      const inp = document.getElementById("chat-text-input");
      const txt = inp.value.trim();
      if (!txt) return;
      const now = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      PATIENTS_DB[activePtKey].chat.push({ from: "doctor", time: now, text: txt });
      inp.value = "";
      renderChatMessages(PATIENTS_DB[activePtKey]);
    });

    // Выдвижной технический инспектор R&D для жюри
    const drawer = document.getElementById("rd-inspector-drawer");
    document.getElementById("btn-toggle-rd-drawer").addEventListener("click", () => {
      drawer.classList.toggle("open");
    });
    document.getElementById("btn-close-rd-drawer").addEventListener("click", () => {
      drawer.classList.remove("open");
    });

    const rulesConfig = {
      engine: "COMPASS-HYBRID v2.6 Hot-Reload",
      grls_drug: "Авиандр® (Маритупирдин, таб. 20 мг, ГК ХимРар)",
      rules: [
        { id: "R-MAOI", trigger: "co_med in ['Транилципромин', 'Фенелзин']", routing: "ОБЯЗАТЕЛЬНО" },
        { id: "R-PK-CMAX", trigger: "c_max_ng_ml >= 320.0", routing: "ОБЯЗАТЕЛЬНО" },
        { id: "R-DISSIM", trigger: "dempster_conflict_k >= 0.42 and dissim_idx >= 0.35", routing: "ОБЯЗАТЕЛЬНО" }
      ]
    };
    document.getElementById("rd-rules-json").value = JSON.stringify(rulesConfig, null, 2);
  }

  window.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    loadPatientIntoChart("pt_sokolova");
  });
})();