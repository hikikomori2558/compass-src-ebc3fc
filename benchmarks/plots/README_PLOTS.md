# Научные графики (benchmarks/plots/)

Графики генерируются скриптами автоматически (PNG 300 DPI + SVG, вывод — прямо в эту папку):

1. `fig1_pk_cyp2d6_168h` — Фармакокинетический профиль Авиандр® (20 мг 2 р/сут за 30 мин до еды, 168 ч RK4 C-Core, CYP2D6 Normal vs Poor Metabolizer).
2. `fig2_sechenov_36w_trajectory` — Мониторинг динамики терапии Авиандр® относительно 36-недельного коридора РКИ Сеченовского университета (N=98, SIGH-A).
3. `fig3_umap_24d_clusters` — Стратификация 24-мерных векторов состояния пациентов (z_t ∈ R^24, детерминированные клинические правила + DTW, кластеры К1–К5).
4. `fig4_custdev_doctors` — CustDev врачей (N=18): рутина приёма, дропаут, красные флаги, каналы алертов.

Перегенерация:

```bash
python3 benchmarks/generate_scientific_plots.py   # fig1–fig3
python3 benchmarks/generate_custdev_study.py      # fig4 (+ JSON отчёта)
# или VS Code: Cmd+Shift+P -> Tasks: Run Task -> «6. Научные графики»
```
