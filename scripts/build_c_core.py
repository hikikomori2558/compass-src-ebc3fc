#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Кроссплатформенный сборщик нативного ядра libcompass_core.so
Автоматически определяет macOS (Apple Silicon M-series) / Linux / Windows.
"""

import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C_DIR = ROOT / "c_core"
OUT_LIB = C_DIR / "libcompass_core.so"

def main():
    c_files = sorted(C_DIR.glob("*.c"))
    if not c_files:
        print("[!] В папке c_core/ пока нет .c файлов (ожидаем заливку C-Core от Михаила).")
        sys.exit(0)

    arch = platform.machine().lower()
    sys_name = platform.system()

    cmd = ["gcc", "-O3", "-shared", "-fPIC"]
    if "x86_64" in arch or "amd64" in arch:
        cmd.append("-mavx2")
    elif "arm" in arch or "aarch64" in arch:
        cmd.append("-mcpu=native")

    cmd.extend([str(f) for f in c_files])
    cmd.extend(["-o", str(OUT_LIB), "-lm"])

    print(f"[*] Компиляция C-Core ({sys_name} / {arch}):")
    print("    " + " ".join(cmd))
    t0 = time.perf_counter()
    res = subprocess.run(cmd, capture_output=True, text=True)
    dt = (time.perf_counter() - t0) * 1000.0

    if res.returncode != 0:
        print("[ERROR] Ошибка компиляции:\n", res.stderr)
        sys.exit(res.returncode)

    print(f"[OK] Собрана библиотека {OUT_LIB.name} за {dt:.1f} мс.")

if __name__ == "__main__":
    main()
