# backend/bridges/loader.py — единая точка загрузки libcompass_core.so для всех C-модулей.
#
# Зачем один загрузчик: pii_scrubber и simd_lexer живут в одной библиотеке; у каждого
# свой биндинг функций, но сборка/пересборка/кэш CDLL должны быть общими — иначе два
# модуля устраивают гонку «кто первый пересоберёт .so» при первом одновременном заходе.
import ctypes
import subprocess
import sys
import threading
from typing import Optional

from backend.paths import C_CORE_DIR, CORE_LIB, REPO_ROOT

_LOCK = threading.Lock()
_LIB: Optional[ctypes.CDLL] = None

# Сигнатурные символы всех C-модулей ядра: если хоть одного нет в .so — библиотека
# устарела и подлежит пересборке (так .so, собранный до вчерашнего дня, не «протухнет» тихо).
SIGNATURE_SYMBOLS = ("pii_scrubber_new", "lexer_scan", "evaluate_compass_core_v26")


def load_core_lib() -> ctypes.CDLL:
    """Загружает и кэширует CDLL; при отсутствии/устаревании — пересобирает под эту ОС."""
    global _LIB
    with _LOCK:  # два потока не должны пересобирать ядро одновременно
        if _LIB is not None:
            return _LIB

        def _load() -> ctypes.CDLL:
            return ctypes.CDLL(str(CORE_LIB))

        if not CORE_LIB.exists():
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "build_c_core.py")], check=True)
            _LIB = _load()
            return _LIB

        lib = _load()
        if not all(hasattr(lib, sym) for sym in SIGNATURE_SYMBOLS):
            # .so есть, но собран до появления части модулей — пересборка и перезагрузка
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "build_c_core.py")], check=True)
            lib = _load()
        _LIB = lib
        return _LIB


def resolve_bridge_lib(own_lib_prefix: str, symbols: tuple) -> Path:
    """Путь к библиотеке с символами моста — БЕЗ вызова компилятора там, где его нет.

    Порядок (единая философия: один .so на всё ядро):
      1. libcompass_core.so уже экспортирует символы моста (build_c_core собирает
         ВСЕ c_core/*.c разом — так живёт multi-stage-образ без gcc в рантайме);
      2. собственная отдельная .so моста (dev-режим, собранная ранее cc/gcc).
    Компиляцию в рантайме не делаем принципиально: образ собирается без gcc.
    """
    core = load_core_lib()
    if all(hasattr(core, sym) for sym in symbols):
        return CORE_LIB

    ext = "dylib" if sys.platform == "darwin" else "so"
    own = C_CORE_DIR / f"{own_lib_prefix}.{ext}"
    if own.exists():
        lib = ctypes.CDLL(str(own))
        if all(hasattr(lib, sym) for sym in symbols):
            return own
    raise RuntimeError(
        f"Символы {symbols[0]}... не найдены ни в libcompass_core.so, ни в {own.name}. "
        f"Запусти: python3 scripts/build_c_core.py"
    )
