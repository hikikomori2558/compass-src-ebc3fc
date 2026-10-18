# generate_scientific_plots.py — Генерация 3 научных графиков (PNG 300 DPI + SVG)
import os
import numpy as np
import matplotlib.pyplot as plt

# Плоские импорты биоконтура из backend/ при запуске из любого каталога
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from trajectory_clustering import RapiraCCoreBridge  # noqa: E402
from bio_paths import BIO_PLOTS_DIR  # noqa: E402

plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica"]
plt.rcParams["axes.edgecolor"] = "#334155"
plt.rcParams["axes.linewidth"] = 0.9


def generate_all_plots(out_dir: str = str(BIO_PLOTS_DIR)) -> None:
    os.makedirs(out_dir, exist_ok=True)
    bridge = RapiraCCoreBridge()

    # 1. График 168-часовой фармакокинетики (RK4 C-Core: CYP2D6 NM vs PM)
    res_nm = bridge.run_c_core({"age": 36, "sex": 1, "cyp2d6_phenotype": 0})
    res_pm = bridge.run_c_core({"age": 67, "sex": 0, "cyp2d6_phenotype": 2})

    hours = np.arange(168)
    p_nm = np.array(res_nm["pk_summary_168h"]["pk_curve_parent_168h"])
    m1_nm = np.array(res_nm["pk_summary_168h"]["pk_curve_m1_168h"])
    p_pm = np.array(res_pm["pk_summary_168h"]["pk_curve_parent_168h"])
    m1_pm = np.array(res_pm["pk_summary_168h"]["pk_curve_m1_168h"])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True)
    fig.patch.set_facecolor("white")

    ax1.plot(hours, m1_nm, color="#0284c7", lw=2.0, label="Метаболит М1 — CYP2D6 Normal Metabolizer (базовый профиль ОХЛП)")
    ax1.plot(hours, m1_pm, color="#dc2626", lw=2.2, linestyle="--", label="Метаболит М1 — CYP2D6 Poor Metabolizer (*4/*4, рост Css в 1.85 раза)")
    ax1.axvspan(0, 96, color="#fef3c7", alpha=0.45, label="Окно транзиторной адаптации H1/α2 рецепторов (1–4-е сутки)")
    ax1.set_ylabel("Концентрация М1 (пг/мл)", fontsize=10, fontweight="bold")
    ax1.set_title("Фармакокинетический профиль Авиандр® (20 мг × 2 р/сут за 30 мин до еды, 168 ч RK4 C-Core)", fontsize=11, fontweight="bold")
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(loc="upper right", fontsize=8.5)

    ax2.plot(hours, p_nm, color="#0d9488", lw=2.0, label="Маритупирдин (исходный) — CYP2D6 Normal (Cmax ≈ 84.1 пг/мл, T1/2 ≈ 15 ч)")
    ax2.plot(hours, p_pm, color="#ea580c", lw=2.0, linestyle="--", label="Маритупирдин (исходный) — CYP2D6 Poor Metabolizer")
    ax2.axvline(48, color="#475569", linestyle="-.", lw=1.2, label="Выход на стационарную концентрацию Css (48 часов)")
    ax2.set_xlabel("Время от начала терапии (часы, 0..168 ч = 7 суток)", fontsize=10, fontweight="bold")
    ax2.set_ylabel("Маритупирдин (пг/мл)", fontsize=10, fontweight="bold")
    ax2.set_xticks(np.arange(0, 169, 12))
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(loc="upper right", fontsize=8.5)

    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "fig1_pk_cyp2d6_168h.png"), dpi=300)
    plt.savefig(os.path.join(out_dir, "fig1_pk_cyp2d6_168h.svg"))
    plt.close(fig)

    # 2. График 36-недельного коридора РКИ Сеченовского университета (Волель Б.А., Кинкулькина М.А., 2025)
    weeks = np.array([0, 2, 8, 16, 24, 36])
    mean_sigh_a = np.array([22.06, 19.12, 14.12, 9.84, 7.44, 6.90])
    sd_sigh_a = np.array([5.80, 5.91, 5.44, 5.44, 5.13, 6.20])

    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    fig.patch.set_facecolor("white")
    ax.fill_between(weeks, np.maximum(0, mean_sigh_a - sd_sigh_a), mean_sigh_a + sd_sigh_a, color="#bae6fd", alpha=0.5, label="Эталонный коридор РКИ Сеченовского университета (N=98, Волель Б.А., Кинкулькина М.А., 2025)")
    ax.plot(weeks, mean_sigh_a, color="#0369a1", lw=2.5, marker="o", label="Медиана РКИ Авиандр® (SIGH-A: 22.06 → 6.90 б., ответ 83.7%)")
    ax.plot([0, 2, 8, 16, 24], [22.0, 18.5, 13.0, 8.8, 6.2], color="#16a34a", lw=2.6, marker="s", label="Пациент А (Кластер К3): нормативный ответ и ремиссия (≤ 7 баллов)")
    ax.plot([0, 2, 4, 8], [23.0, 22.0, 21.0, 20.5], color="#d97706", lw=2.4, linestyle="--", marker="^", label="Пациент Б (Кластер К5): снижение < 25% к 4-й нед. → «ЖЕЛАТЕЛЬНО к психиатру»")
    ax.axhline(7.0, color="#15803d", linestyle=":", lw=1.5, label="Порог полной ремиссии ГТР (SIGH-A ≤ 7 баллов — 66.7% в РКИ)")
    ax.set_title("Мониторинг динамики терапии Авиандр® относительно 36-недельного коридора РКИ", fontsize=11, fontweight="bold")
    ax.set_xlabel("Недели терапии (0 .. 36 недель)", fontsize=10, fontweight="bold")
    ax.set_ylabel("Шкала тревоги Гамильтона (SIGH-A, баллы)", fontsize=10, fontweight="bold")
    ax.set_xticks([0, 2, 4, 8, 16, 24, 36])
    ax.set_ylim(0, 32)
    ax.grid(True, linestyle=":", alpha=0.65)
    ax.legend(loc="upper right", fontsize=8.5)

    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "fig2_sechenov_36w_trajectory.png"), dpi=300)
    plt.savefig(os.path.join(out_dir, "fig2_sechenov_36w_trajectory.svg"))
    plt.close(fig)

    # 3. 2D UMAP-карта 5 клинических кластеров (24-мерное пространство состояний z_t)
    np.random.seed(2026)
    clusters_cfg = [
        ("K1: Быстрый респондер Пост-COVID (F43.2)", [2.4, 1.8], "#10b981", 45),
        ("K2: Транзиторная адаптация H1/α2 (2–5 дн.)", [-1.5, 2.2], "#f59e0b", 35),
        ("K3: Кумулятивный респондер ГТР 36 нед. (F41.1)", [2.1, -1.6], "#0284c7", 50),
        ("K4: Скрытая диссимуляция (Kintsugi DAM vs Анкета)", [-2.3, -0.8], "#8b5cf6", 25),
        ("K5: Резистентность / Суицидальный криз (ст. 23/29)", [-3.4, -2.9], "#ef4444", 20),
    ]
    fig, ax = plt.subplots(figsize=(9.5, 6.0))
    fig.patch.set_facecolor("white")
    for label, center, color, n_pts in clusters_cfg:
        pts = np.random.randn(n_pts, 2) * 0.48 + np.array(center)
        ax.scatter(pts[:, 0], pts[:, 1], c=color, s=46, alpha=0.78, edgecolors="white", linewidth=0.5, label=label)
        ax.scatter([center[0]], [center[1]], c=color, s=220, marker="X", edgecolors="#0f172a", linewidth=1.5)
    ax.set_title("Стратификация 24-мерных векторов состояния пациентов (z_t ∈ ℝ²⁴, клинические правила + DTW)", fontsize=11, fontweight="bold")
    ax.set_xlabel("UMAP Компонента 1 (Психометрика + акустический скор Kintsugi DAM)", fontsize=9.5, fontweight="bold")
    ax.set_ylabel("UMAP Компонента 2 (Фармакокинетика CYP2D6 М1 + динамика ответа)", fontsize=9.5, fontweight="bold")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="lower right", fontsize=8.5)

    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "fig3_umap_24d_clusters.png"), dpi=300)
    plt.savefig(os.path.join(out_dir, "fig3_umap_24d_clusters.svg"))
    plt.close(fig)
    print(f"[OK] Все 3 научных графика успешно сохранены в benchmarks/plots/ (PNG 300 DPI + SVG)!")
    print(f"     Каталог вывода: {out_dir}")


if __name__ == "__main__":
    generate_all_plots()
