# tests/test_pk_calibration.py — страж калибровки PK-модели по ОХЛП «Авиандр®».
#
# Фиксирует числовые инварианты, на которые опираются слайды и интерфейс:
# если кто-то поменяет константы решателя, тест упадёт ДО того, как на демо
# разойдутся цифры. Прогон: python3 -m tests.test_pk_calibration
import sys

from backend.bridges.core import RapiraCCoreBridge

failures = []


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    b = RapiraCCoreBridge()
    nm = b.run_c_core({"age": 36, "sex": 1, "serum_creatinine_umol_l": 86.0, "cyp2d6_phenotype": 0})
    pm = b.run_c_core({"age": 36, "sex": 1, "serum_creatinine_umol_l": 86.0, "cyp2d6_phenotype": 2})
    pk_n, pk_p = nm["pk_summary_168h"], pm["pk_summary_168h"]

    # 1. Родитель: Cmax,ss = 84.1 пг/мл (ОХЛП), T1/2 = 15 ч, Css к 48 ч
    check("Cmax родителя = 84.1 ± 5%", abs(pk_n["parent_cmax_pg_ml"] - 84.1) < 84.1 * 0.05,
          f"{pk_n['parent_cmax_pg_ml']:.1f}")
    tr48 = pk_n["pk_curve_parent_168h"][47] / pk_n["parent_ctrough_ss_pg_ml"]
    check("Выход на Css к 48 ч (>= 85% плато)", tr48 >= 0.85, f"{tr48*100:.0f}%")

    # 2. M1: T1/2 = 4.7 ч, AUC_M1/AUC_P = 2.9 (в окне 168 ч допустимо 2.6–3.1)
    auc_p = sum(pk_n["pk_curve_parent_168h"])
    auc_m = sum(pk_n["pk_curve_m1_168h"])
    ratio = auc_m / auc_p
    check("AUC_M1/AUC_P = 2.9 ± 10%", 2.9 * 0.9 <= ratio <= 2.9 * 1.1, f"{ratio:.2f}")

    # 3. Честная метка: Cmax-отношение в модели ≈ 2.4; ОХЛП публикует 6.9 —
    # значение вне линейной модели, поэтому проверяем НЕ 6.9, а отсутствие фальши:
    # модельное отношение должно быть монотонно больше 1 (M1 доминирует в плазме).
    cmax_ratio = pk_n["m1_cmax_pg_ml"] / pk_n["parent_cmax_pg_ml"]
    check("Cmax_M1 > Cmax_родителя (качественно)", cmax_ratio > 1.5,
          f"{cmax_ratio:.2f} (ОХЛП: 6.9, см. примечание модели)")

    # 4. PM: рост Ctrough/Cavg родителя и M1 в 1.5–2 раза (ОХЛП)
    for label, key in (("родителя", "parent_ctrough_ss_pg_ml"), ("M1", "m1_ctrough_ss_pg_ml")):
        r = pk_p[key] / pk_n[key]
        check(f"PM: Ctrough {label} в коридоре 1.5–2.0", 1.5 <= r <= 2.0, f"x{r:.2f}")
    r_cmax = pk_p["parent_cmax_pg_ml"] / pk_n["parent_cmax_pg_ml"]
    check("PM: Cmax родителя в коридоре 1.5–2.0", 1.5 <= r_cmax <= 2.0, f"x{r_cmax:.2f}")

    # 5. Феноконверсия: сильный ингибитор CYP2D6 у NM поднимает экспозицию,
    #    DDI-предупреждение выставлено, безопасные комбинации — отдельно.
    ph = b.run_c_core({"age": 36, "sex": 1, "serum_creatinine_umol_l": 86.0,
                       "cyp2d6_phenotype": 0, "comedication_bitmask": 1 << 4})  # сильный ингибитор
    check("Феноконверсия: экспозиция выросла (мульт. > 1.4)",
          ph["cyp2d6_exposure_multiplier"] > 1.4, f"{ph['cyp2d6_exposure_multiplier']}")
    check("Феноконверсия: DDI-предупреждение выставлено",
          ph["ddi_warning_bitmask"] & (1 << 4) != 0)
    safe = b.run_c_core({"age": 36, "sex": 1, "serum_creatinine_umol_l": 86.0,
                         "comedication_bitmask": (1 << 0) | (1 << 2)})  # метопролол + ципрофлоксацин
    check("Верифицированно безопасные DDI (метопролол, ципрофлоксацин)",
          safe["ddi_verified_safe_bitmask"] == (1 << 0) | (1 << 2)
          and "ВЕРИФИЦИРОВАННАЯ БЕЗОПАСНОСТЬ" in " ".join(safe["endocrine_and_somatic_masks"]))

    print()
    if failures:
        print(f"[FAIL] Калибровка нарушена: {failures}")
        return 1
    print("[OK] Все инварианты калибровки PK по ОХЛП выполнены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
