# backend/pii_scrubber.py — ctypes-мост к c_core/pii_scrubber.c (152-ФЗ, Zero-PII)
# Использование:
#   from pii_scrubber import get_scrubber
#   clean, mask, kinds = get_scrubber().scrub("Иванов И.И., +7 (913) 123-45-67")
import ctypes
import subprocess
import sys
from pathlib import Path
from typing import Optional

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from bio_paths import CORE_LIB, REPO_ROOT  # noqa: E402

SIZE_T_MAX = ctypes.c_size_t(-1).value

KIND_BITS = [
    (1, "phone"),
    (2, "snils"),
    (4, "oms_policy"),
    (8, "email"),
    (16, "fio"),
]


class PiiScrubber:
    def __init__(self, pepper: str = "dev_pepper_change_me"):
        self.pepper = pepper.encode("utf-8")
        self._lib = self._load_lib()
        fn = self._lib.pii_scrub_stream
        fn.argtypes = [
            ctypes.c_char_p,               # in
            ctypes.c_char_p,               # out
            ctypes.c_size_t,               # out_cap
            ctypes.c_char_p,               # pepper
            ctypes.POINTER(ctypes.c_uint32),  # found_mask
        ]
        fn.restype = ctypes.c_size_t
        self._scrub_fn = fn

    @staticmethod
    def _load_lib():
        if not CORE_LIB.exists():
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "build_c_core.py")], check=True)
        lib = ctypes.CDLL(str(CORE_LIB))
        try:
            lib.pii_scrub_stream
        except AttributeError:
            # библиотека собрана до добавления pii_scrubber.c — пересборка
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "build_c_core.py")], check=True)
        return ctypes.CDLL(str(CORE_LIB))

    def scrub(self, text: str):
        """Возвращает (clean_text, found_mask, kinds_dict). Псевдоним: PII_<hmac8(pepper, фрагмент)>."""
        raw = text.encode("utf-8")
        cap = 3 * len(raw) + 128
        mask = ctypes.c_uint32(0)
        while True:
            buf = ctypes.create_string_buffer(cap)
            n = self._scrub_fn(raw, buf, cap, self.pepper, ctypes.byref(mask))
            if n != SIZE_T_MAX:
                return buf.raw[:n].decode("utf-8"), mask.value, {
                    name: bool(mask.value & bit) for bit, name in KIND_BITS
                }
            cap *= 2


_SCRUBBER: Optional[PiiScrubber] = None


def get_scrubber() -> PiiScrubber:
    global _SCRUBBER
    if _SCRUBBER is None:
        import os

        _SCRUBBER = PiiScrubber(os.getenv("PII_PEPPER", "dev_pepper_change_me"))
    return _SCRUBBER


if __name__ == "__main__":
    demo = "Пациент Иванов И.И., 45 лет, тел. +7 (913) 123-45-67, СНИЛС 123-456-789 00, ivanov@mail.ru."
    clean, mask, kinds = get_scrubber().scrub(demo)
    print("БЫЛО:  ", demo)
    print("СТАЛО: ", clean)
    print("Категории:", kinds)
