# backend/c_lib.py — единая точка загрузки libcompass_core.so для всех C-модулей.
#
# Зачем один загрузчик: pii_scrubber и simd_lexer живут в одной библиотеке; у каждого
# свой биндинг функций, но сборка/пересборка/кэш CDLL должны быть общими — иначе два
# модуля устраивают гонку «кто первый пересоберёт .so» при первом одновременном заходе.
import ctypes
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from bio_paths import CORE_LIB, REPO_ROOT  # noqa: E402

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
