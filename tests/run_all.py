# tests/run_all.py — прогон ВСЕХ функциональных сьютов одним входом.
# Каждый suite — самостоятельный модуль с ненулевым exit-кодом при провале.
# Смоук/нагрузочные замеры, требующие внешней инфры (Postgres/Redis/API),
# помечены как SKIP, если инфра недоступна.
import os
import socket
import subprocess
import sys


def _pg_up() -> bool:
    """Postgres: репо-конвенция 5433 (docker) или нативный 5432."""
    return any(_port_open("127.0.0.1", p) for p in (5433, 5432))


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False

SUITES = [
    # (модуль, требует внешнюю инфру?)
    ("tests.test_c_core_smoke", False),
    ("tests.test_pk_calibration", False),
    ("tests.test_domain_rules", False),
    ("tests.test_domain_guidance", False),
    ("tests.test_domain_axes_and_guard", False),
    ("tests.test_c_lexer_ahocorasik", False),
    ("tests.test_c_pii_scrubber", False),
    ("tests.test_safe_pipeline", False),
    ("tests.test_c_pk_rk45_bitap", False),
    ("tests.test_c_ot_xai_ood", False),
    ("tests.test_api_cdss_workstation", False),
    ("tests.test_api_auth", True),          # требует Postgres
    ("tests.test_api_alerts_ws", True),     # требует Postgres + Redis
    ("tests.test_db_rls", True),            # требует Postgres + seed
]


def main() -> int:
    passed, skipped, failed = [], [], []
    infra_up = _pg_up() and _port_open("127.0.0.1", 6379)
    if infra_up and _port_open("127.0.0.1", 5432) and not _port_open("127.0.0.1", 5433):
        # нативный Postgres на 5432 (VM без Docker): подставляем DSN детям,
        # если пользователь не задал свои
        native = "postgresql://compass_api:compass_api_demo@localhost:5432/compass"
        native_owner = "postgresql://compass_app:compass_demo@localhost:5432/compass"
        os.environ.setdefault("DATABASE_URL", native)
        os.environ.setdefault("APP_DATABASE_URL", native)
        os.environ.setdefault("OWNER_DATABASE_URL", native_owner)
    for module, needs_infra in SUITES:
        r = subprocess.run([sys.executable, "-m", module], capture_output=True, text=True)
        name = module.replace("tests.", "")
        if needs_infra and not infra_up:
            skipped.append(name)
            print(f"[SKIP] {name} — нет Postgres/Redis (запусти docker compose up -d)")
            continue
        if r.returncode == 0:
            passed.append(name)
            print(f"[OK ] {name}")
        else:
            failed.append(name)
            print(f"[FAIL] {name}\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    print(f"\nИТОГО: прошло {len(passed)}, пропущено без инфры {len(skipped)}, упало {len(failed)}")
    if failed:
        print("Упавшие:", ", ".join(failed))
        return 1
    print("ВСЕ ДОСТУПНЫЕ СЬЮТЫ ЗЕЛЁНЫЕ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
