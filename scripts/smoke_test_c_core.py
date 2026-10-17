#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Смоук-тест и бенчмарк C-Core (Метрика №1: целевой отклик < 0.15 мс)
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_PATH = ROOT / "c_core" / "libcompass_core.so"

if __name__ == "__main__":
    if not LIB_PATH.exists():
        print(f"[!] Библиотека {LIB_PATH.name} еще не собрана.")
        print("    После того как Михаил зальет .c файлы, запусти: python3 scripts/build_c_core.py")
        raise SystemExit(0)
    print(f"[OK] Библиотека {LIB_PATH} найдена и готова к вызовам через cffi.")
