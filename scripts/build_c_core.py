#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка нативного ядра libcompass_core.so — одна команда на любой машине команды.
Под macOS на Apple Silicon и на x86-линуксе флаги подбираются сами, так что
«у меня не компилируется» больше не аргумент. Библиотека в git не живёт (.gitignore),
каждый собирает её локально под свою платформу.
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
    # Берём вообще все .c из c_core/ — новое C-модули подхватываются без правок скрипта.
    c_files = sorted(C_DIR.glob("*.c"))
    if not c_files:
        print("[!] В c_core/ пусто — собирать нечего.")
        sys.exit(0)

    arch = platform.machine().lower()
    sys_name = platform.system()

    # -Wall -Wextra держим всегда: предупреждений в c_core быть не должно в принципе,
    # это же медицинское ядро. Новые модули, которые шумят, — чиним сразу, а не глушим.
    cmd = ["gcc", "-std=c11", "-O3", "-Wall", "-Wextra", "-shared", "-fPIC"]
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
    if res.stderr.strip():
        # Сборка прошла, но компилятор что-то сказал — для нас это повод разобраться,
        # а не машем рукой. Печатаем и роняем сборку: чисто должно быть везде.
        print("[ERROR] Компилятор выдал предупреждения — это блокер для c_core:")
        print(res.stderr)
        sys.exit(1)

    print(f"[OK] Собрана библиотека {OUT_LIB.name} за {dt:.1f} мс (0 предупреждений).")

if __name__ == "__main__":
    main()
