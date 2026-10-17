# backend/pii_scrubber.py — мост к c_core/pii_scrubber.c (контур Zero-PII, Разработчик №1).
#
# Пользоваться просто:
#     from pii_scrubber import get_scrubber
#     clean, mask, kinds = get_scrubber().scrub("Иванов И.И., +7 (913) 123-45-67")
# clean — текст, в котором ПДн заменены на псевдонимы PII_xxxxxxxx; mask — битовая маска
# найденных категорий; kinds — то же самое, но по-человечески, словарём.
#
# Перец берётся из PII_PEPPER (по умолчанию девелоперский — для боевого сменить обязательно).
# Кэш HMAC-состояний живёт в C-структуре ровно один инстанс на перец, поэтому каждый
# вызов scrub — это только скан текста, без пересборки ключей.
import atexit
import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from bio_paths import CORE_LIB, REPO_ROOT  # noqa: E402

SIZE_T_MAX = ctypes.c_size_t(-1).value

# Порядок важен: биты совпадают с PII_FOUND_* из pii_scrubber.h.
KIND_BITS = [
    (1, "phone"),
    (2, "snils"),
    (4, "oms_policy"),
    (8, "email"),
    (16, "fio"),
]


def _load_lib() -> ctypes.CDLL:
    """Грузит libcompass_core.so; если собирали до появления pii_scrubber — пересобирает."""
    def load():
        return ctypes.CDLL(str(CORE_LIB))

    if not CORE_LIB.exists():
        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "build_c_core.py")], check=True)
        return load()
    lib = load()
    if not hasattr(lib, "pii_scrubber_new"):
        # в .so ещё нет нового API — пересобираем c_core и грузим заново
        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "build_c_core.py")], check=True)
        lib = load()
    return lib


def _bind(lib: ctypes.CDLL) -> None:
    lib.pii_scrubber_new.argtypes = [ctypes.c_char_p]
    lib.pii_scrubber_new.restype = ctypes.c_void_p
    lib.pii_scrubber_free.argtypes = [ctypes.c_void_p]
    lib.pii_scrubber_free.restype = None
    lib.pii_scrubber_scrub.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.pii_scrubber_scrub.restype = ctypes.c_size_t
    # легаси-точка — держим привязанной для A/B-проверок и фоллбэка
    lib.pii_scrub_stream.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.pii_scrub_stream.restype = ctypes.c_size_t


class PiiScrubber:
    def __init__(self, pepper: str = "dev_pepper_change_me"):
        self.pepper = pepper.encode("utf-8")
        self._lib = _load_lib()
        _bind(self._lib)
        self._ctx = self._lib.pii_scrubber_new(self.pepper)
        if not self._ctx:
            raise RuntimeError("pii_scrubber_new вернул NULL — пустой перец?")
        self._buf = None   # переиспользуемый выходной буфер, растём только когда не влезает
        atexit.register(self._destroy)

    def _destroy(self) -> None:
        if getattr(self, "_ctx", None):
            self._lib.pii_scrubber_free(self._ctx)
            self._ctx = None

    def scrub_bytes(self, raw: bytes) -> Tuple[bytes, int, Dict[str, bool]]:
        """Горячий путь без UTF-8-кодеков: bytes -> bytes. Буфер живёт в инстансе,
        поэтому повторные вызовы не платят за аллокацию и обнуление нескольких мегабайт."""
        mask = ctypes.c_uint32(0)
        cap = 2 * len(raw) + 128
        if self._buf is None or len(self._buf) < cap:
            self._buf = ctypes.create_string_buffer(cap)
        while True:
            n = self._lib.pii_scrubber_scrub(self._ctx, raw, self._buf, len(self._buf), ctypes.byref(mask))
            if n != SIZE_T_MAX:
                # memoryview-слайс вместо .raw: не тащим хвост буфера за собой
                out = memoryview(self._buf)[:n].tobytes()
                kinds = {name: bool(mask.value & bit) for bit, name in KIND_BITS}
                return out, mask.value, kinds
            self._buf = ctypes.create_string_buffer(len(self._buf) * 2)

    def scrub(self, text: str) -> Tuple[str, int, Dict[str, bool]]:
        """Удобная обёртка для текста: кодируем, чистим, декодируем обратно."""
        out, mask, kinds = self.scrub_bytes(text.encode("utf-8"))
        return out.decode("utf-8"), mask, kinds


def legacy_scrub(text: str, pepper: str) -> Tuple[str, int, Dict[str, bool]]:
    """Разовый вызов без кэша — оставлен для A/B-теста эквивалентности в benchmarks."""
    lib = _load_lib()
    _bind(lib)
    raw = text.encode("utf-8")
    cap = 2 * len(raw) + 128
    mask = ctypes.c_uint32(0)
    while True:
        buf = ctypes.create_string_buffer(cap)
        n = lib.pii_scrub_stream(raw, buf, cap, pepper.encode("utf-8"), ctypes.byref(mask))
        if n != SIZE_T_MAX:
            kinds = {name: bool(mask.value & bit) for bit, name in KIND_BITS}
            return buf.raw[:n].decode("utf-8"), mask.value, kinds
        cap *= 2


_SCRUBBER: Optional[PiiScrubber] = None


def get_scrubber() -> PiiScrubber:
    global _SCRUBBER
    if _SCRUBBER is None:
        _SCRUBBER = PiiScrubber(os.getenv("PII_PEPPER", "dev_pepper_change_me"))
    return _SCRUBBER


if __name__ == "__main__":
    demo = "Пациент Иванов И.И., 45 лет, тел. +7 (913) 123-45-67, СНИЛС 123-456-789 00, ivanov@mail.ru."
    clean, mask, kinds = get_scrubber().scrub(demo)
    print("БЫЛО:  ", demo)
    print("СТАЛО: ", clean)
    print("Категории:", kinds)
