# benchmarks/generate_custdev_study.py
"""
Исследование №1 (Полевой CustDev врачей, N = 18 респондентов: 10 неврологов/терапевтов, 8 психиатров).
Генерирует JSON-отчет в clinical_data/custdev_doctors_n18_results.json
и 4-панельную научную диаграмму в benchmarks/plots/fig4_custdev_doctors.png (.svg).
"""
import json
from pathlib import Path
from typing import Dict, Any
import numpy as np
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[1]
CLINICAL_DATA_DIR = REPO_ROOT / "clinical_data"
PLOTS_DIR = REPO_ROOT / "benchmarks" / "plots"

plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica"]
plt.rcParams["axes.edgecolor"] = "#334155"
plt.rcParams["axes.linewidth"] = 0.9


def build_custdev_dataset() -> Dict[str, Any]:
    return {
        "study_id": "RAPIRA_CUSTDEV_2026_10",
        "period": "15–21 октября 2026 г.",
        # Чек-лист кейса (п. 4): цифры без методологии в презентацию не идут.
        # Статус меняется на CONFIRMED, когда Михаил приложит способ сбора (анкета/
        # интервью), даты и роли опрошенных. Сами цифры не трогать.
        "methodology_status": "TO_CONFIRM_WITH_TEAM",
        "methodology_note": (
            "Агрегаты внесены вручную (М. Журавлёв). До сдачи 27.10 подтвердить формат "
            "сбора, роли и фактическое число респондентов; если собрано меньше 18 — "
            "указать реальный n в презентации."
        ),
        "total_respondents": 18,
        "specialties_breakdown": {
            "neurologists_and_gps": 10,
            "psychiatrists_and_psychotherapists": 8,
        },
        "q1_intake_routine_time_minutes": {
            "question": "Сколько минут на первичном приеме уходит на сбор анамнеза, перепечатывание в МИС и подсчет шкал (HADS/PHQ-9/GAD-7)?",
            "mean_minutes": 8.8,
            "sd_minutes": 2.1,
            "distribution_buckets": {
                "< 5 мин": 1,
                "5–7 мин": 3,
                "8–10 мин": 10,
                "> 10 мин": 4,
            },
            "key_insight": "У 77.8% врачей рутинное оформление шкал и проверка противопоказаний съедает от 8 до 12 минут (до половины амбулаторного приема).",
        },
        "q2_early_dropout_reasons_week_1_2": {
            "question": "Какая доля пациентов с тревогой/ГТР самостоятельно бросает прием препаратов в первые 2 недели и почему?",
            "estimated_dropout_rate_pct": 46.5,
            "reasons_share_pct": {
                "Транзиторная сонливость / головокружение в 1–4-й день": 44.0,
                "Ожидание мгновенного эффекта в 1-й день / фармакофобия": 31.0,
                "Нарушение кратности (2 р/сут за 30 мин до еды) и пропуск доз": 16.0,
                "Сопутствующая соматика и страх полипрагмазии": 9.0,
            },
        },
        "q3_mandatory_vs_recommended_red_flags": {
            "question": "По каким главным «красным флагам» невролог понимает, что пациента нужно ОБЯЗАТЕЛЬНО (экстренно), а не ЖЕЛАТЕЛЬНО (планово) передать психиатру?",
            "mandatory_red_flags_consensus_pct": {
                "Суицидальные мысли/намерения (п.9 PHQ-9 >= 1 / C-SSRS >= 1)": 100.0,
                "Психотическая симптоматика (бред, галлюцинации)": 100.0,
                "Тяжелый депрессивный эпизод (PHQ-9 >= 20) или БАР/гипомания (MDQ >= 7)": 94.4,
            },
            "recommended_amber_flags_consensus_pct": {
                "Отсутствие снижения тревоги на >= 25% к 4-й неделе терапии": 88.9,
                "Коморбидная умеренная депрессия (PHQ-9 10–19) и непереносимость СИОЗС": 83.3,
            },
        },
        "q4_preferred_emergency_alert_channel": {
            "question": "Какой канал получения экстренного алерта о критическом состоянии пациента между визитами наиболее удобен врачу?",
            "preferences_pct": {
                "Баннер в сайдбаре МИС/ЭМК + Защищенный Telegram-бот дежурного врача": 61.1,
                "Только компактный виджет поверх ЭМК (без личных мессенджеров)": 27.8,
                "Push-уведомление в мобильном приложении клиники / СМС": 11.1,
            },
        },
        "q5_top_3_dashboard_metrics_in_10_sec": {
            "question": "Какие 3 показателя динамики состояния пациента между визитами врач хочет видеть на первом экране за 10 секунд до повторного приема?",
            "top_metrics_votes_pct": {
                "1. Кривая шкал (GAD-7 / PHQ-9 / SIGH-A) на фоне коридора ответа РКИ": 94.4,
                "2. Реальный комплаенс (PDC %, пропуски доз) и переносимость первых 5 дней": 88.9,
                "3. Красные флаги суицидального риска (C-SSRS) и диссимуляции (голос vs анкета)": 83.3,
                "4. Авто-проверка СКФ (< 60), Чайлд-Пью и взаимодействий (CYP2D6 / метопролол)": 77.8,
            },
        },
    }


def render_fig4_custdev_charts(data: Dict[str, Any]) -> None:
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(13.5, 8.5))
    fig.patch.set_facecolor("white")
    fig.suptitle(
        "Результаты полевого исследования врачей (CustDev команды «Rapira», N = 18: 10 неврологов, 8 психиатров)\n"
        "Валидация клинической проблемы и требований к СППВР «COMPASS-HYBRID v2.6»",
        fontsize=12.5,
        fontweight="bold",
        y=0.98,
    )

    # Панель 1 (Верхняя левая): Потери времени на приеме vs Сайдбар Rapira
    ax1 = axes[0, 0]
    categories_q1 = ["Ручной сбор шкал,\nпересчет СКФ и ОХЛП\n(По данным CustDev)", "Сайдбар Rapira\nповерх ЭМК\n(Замер апробации)"]
    times_min = [8.75, 1.20]
    bars1 = ax1.bar(categories_q1, times_min, color=["#ef4444", "#10b981"], width=0.48, edgecolor="#1e293b")
    ax1.set_ylabel("Затраты времени врача (минуты)", fontsize=9.5, fontweight="bold")
    ax1.set_title("1. Время на скрининг, проверку ОХЛП и маршрутизацию", fontsize=10.5, fontweight="bold")
    ax1.set_ylim(0, 11)
    ax1.grid(axis="y", linestyle=":", alpha=0.6)
    for bar, val in zip(bars1, times_min):
        ax1.text(bar.get_x() + bar.get_width() / 2, val + 0.3, f"{val:.2f} мин", ha="center", fontweight="bold", fontsize=9.5)

    # Панель 2 (Верхняя правая): Причины срыва терапии на 1-2 неделе (почему нужен Кластер К2)
    ax2 = axes[0, 1]
    reasons = list(data["q2_early_dropout_reasons_week_1_2"]["reasons_share_pct"].keys())
    shares = list(data["q2_early_dropout_reasons_week_1_2"]["reasons_share_pct"].values())
    y_pos2 = np.arange(len(reasons))
    bars2 = ax2.barh(y_pos2, shares, color=["#f59e0b", "#0284c7", "#6366f1", "#64748b"], edgecolor="#1e293b")
    ax2.set_yticks(y_pos2)
    ax2.set_yticklabels(reasons, fontsize=8.5)
    ax2.invert_yaxis()
    ax2.set_xlabel("Доля среди причин раннего отказа от терапии (%)", fontsize=9.5, fontweight="bold")
    ax2.set_title("2. Главные причины дропаута пациентов в первые 14 дней", fontsize=10.5, fontweight="bold")
    ax2.set_xlim(0, 55)
    ax2.grid(axis="x", linestyle=":", alpha=0.6)
    for bar, val in zip(bars2, shares):
        ax2.text(val + 1.0, bar.get_y() + bar.get_height() / 2, f"{val:.0f}%", va="center", fontweight="bold", fontsize=9)

    # Панель 3 (Нижняя левая): Консенсус по триггерам «ОБЯЗАТЕЛЬНО» vs «ЖЕЛАТЕЛЬНО» к психиатру
    ax3 = axes[1, 0]
    flags_labels = [
        "[ОБЯЗАТЕЛЬНО] п.9 PHQ-9 / C-SSRS >= 1",
        "[ОБЯЗАТЕЛЬНО] Психотика (ст.29 Закона 3185-1)",
        "[ОБЯЗАТЕЛЬНО] PHQ-9 >= 20 или БАР (MDQ >= 7)",
        "[ЖЕЛАТЕЛЬНО] Ответ < 25% к 4-й неделе",
        "[ЖЕЛАТЕЛЬНО] PHQ-9 10–19 / резист. к СИОЗС",
    ]
    flags_vals = [100.0, 100.0, 94.4, 88.9, 83.3]
    colors3 = ["#dc2626", "#dc2626", "#dc2626", "#d97706", "#d97706"]
    y_pos3 = np.arange(len(flags_labels))
    bars3 = ax3.barh(y_pos3, flags_vals, color=colors3, edgecolor="#1e293b")
    ax3.set_yticks(y_pos3)
    ax3.set_yticklabels(flags_labels, fontsize=8.5)
    ax3.invert_yaxis()
    ax3.set_xlabel("Согласие респондентов-врачей (%)", fontsize=9.5, fontweight="bold")
    ax3.set_title("3. Критерии маршрутизации «Обязательно» vs «Желательно»", fontsize=10.5, fontweight="bold")
    ax3.set_xlim(0, 115)
    ax3.grid(axis="x", linestyle=":", alpha=0.6)
    for bar, val in zip(bars3, flags_vals):
        ax3.text(val + 1.2, bar.get_y() + bar.get_height() / 2, f"{val:.1f}%", va="center", fontweight="bold", fontsize=9)

    # Панель 4 (Нижняя правая): Топ-показатели на первом экране врача за 10 секунд
    ax4 = axes[1, 1]
    top_m_labels = list(data["q5_top_3_dashboard_metrics_in_10_sec"]["top_metrics_votes_pct"].keys())
    top_m_vals = list(data["q5_top_3_dashboard_metrics_in_10_sec"]["top_metrics_votes_pct"].values())
    y_pos4 = np.arange(len(top_m_labels))
    bars4 = ax4.barh(y_pos4, top_m_vals, color="#0d9488", edgecolor="#1e293b")
    ax4.set_yticks(y_pos4)
    ax4.set_yticklabels(top_m_labels, fontsize=8.5)
    ax4.invert_yaxis()
    ax4.set_xlabel("Частота выбора врачами (%)", fontsize=9.5, fontweight="bold")
    ax4.set_title("4. Что врач хочет видеть на экране за 10 с до начала приема", fontsize=10.5, fontweight="bold")
    ax4.set_xlim(0, 110)
    ax4.grid(axis="x", linestyle=":", alpha=0.6)
    for bar, val in zip(bars4, top_m_vals):
        ax4.text(val + 1.2, bar.get_y() + bar.get_height() / 2, f"{val:.1f}%", va="center", fontweight="bold", fontsize=9)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    png_path = PLOTS_DIR / "fig4_custdev_doctors.png"
    svg_path = PLOTS_DIR / "fig4_custdev_doctors.svg"
    plt.savefig(png_path, dpi=300)
    plt.savefig(svg_path)
    plt.close(fig)
    print(f"[OK] График №4 (CustDev врачей) сохранен: {png_path} и {svg_path}")


def main() -> None:
    CLINICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    dataset = build_custdev_dataset()
    json_path = CLINICAL_DATA_DIR / "custdev_doctors_n18_results.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    print(f"[OK] JSON-отчет CustDev (N=18 врачей) сохранен: {json_path}")
    render_fig4_custdev_charts(dataset)


if __name__ == "__main__":
    main()
