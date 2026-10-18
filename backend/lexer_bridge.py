# backend/lexer_bridge.py — мост к c_core/simd_lexer.c (Ахо—Корасик, Разработчик №1).
#
# Роль в архитектуре: лексер отдаёт КАНДИДАТОВ (словарные сущности + позиции в байтах)
# за один проход по тексту; клиническую валидацию контекта делает парсер ЭМК Михаила.
# Порядок в конвейере: сырой текст -> .lower() -> lexer_scan -> кандидаты -> парсер.
#
# Пользуемся просто:
#     from lexer_bridge import get_lexer
#     get_lexer().scan("Код МКБ: F41.1, креат. 74, принимает флуконазол")
# вернёт [{start, length, word, category}, ...]; start/length — в байтах lower-версии.
import ctypes
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from c_lib import load_core_lib  # noqa: E402

SIZE_T_MAX = ctypes.c_size_t(-1).value

# Зеркало LEX_CAT_* из simd_lexer.h (порядок фиксировать, менять только добавлением)
CATEGORY_NAMES = {1: "icd10", 2: "drug", 3: "scale", 4: "abbr"}


class CLexeme(ctypes.Structure):
    # байт-в-байт зеркало lexeme_t из simd_lexer.h (4+2+2, выравнивание совпадает)
    _fields_ = [("start", ctypes.c_uint32),
                ("length", ctypes.c_uint16),
                ("dict_id", ctypes.c_uint16)]


class MedicalLexer:
    def __init__(self) -> None:
        self._lib = load_core_lib()
        lib = self._lib
        lib.lexer_init.restype = ctypes.c_int
        lib.lexer_init.argtypes = []
        lib.lexer_scan.restype = ctypes.c_size_t
        lib.lexer_scan.argtypes = [ctypes.c_char_p,
                                   ctypes.POINTER(CLexeme),
                                   ctypes.c_size_t]
        lib.lexer_dict_size.restype = ctypes.c_uint32
        lib.lexer_dict_size.argtypes = []
        lib.lexer_dict_word.restype = ctypes.c_char_p
        lib.lexer_dict_word.argtypes = [ctypes.c_uint32]
        lib.lexer_dict_cat.restype = ctypes.c_uint8
        lib.lexer_dict_cat.argtypes = [ctypes.c_uint32]

        if lib.lexer_init() != 0:
            raise RuntimeError("lexer_init: автомат не построился (не хватило памяти?)")
        # Словарь живёт в C (одна копия правды); сюда берём зеркало один раз на процесс.
        self.dict: List[Dict] = []
        for i in range(lib.lexer_dict_size()):
            self.dict.append({"word": lib.lexer_dict_word(i).decode("utf-8"),
                              "category": CATEGORY_NAMES.get(lib.lexer_dict_cat(i), "?")})

    def scan(self, text: str) -> List[Dict]:
        """Один проход Ахо—Корасика. Регистр: ASCII сворачивается в C, кириллица —
        здесь (.lower()), так что можно скармливать исходный текст врача."""
        raw = text.lower().encode("utf-8")
        cap = 4096
        while True:
            buf = (CLexeme * cap)()
            n = self._lib.lexer_scan(raw, buf, cap)
            if n != SIZE_T_MAX:
                break
            cap *= 2  # не влезло (экзотика: текст-рекордсмен) — удваиваем и заново
        out: List[Dict] = []
        for i in range(n):
            e = buf[i]
            out.append({"start": int(e.start),
                        "length": int(e.length),
                        "word": self._lib.lexer_dict_word(e.dict_id).decode("utf-8"),
                        "category": CATEGORY_NAMES.get(self._lib.lexer_dict_cat(e.dict_id), "?")})
        return out


_LEXER: Optional[MedicalLexer] = None
_LEXER_LOCK = threading.Lock()


def get_lexer() -> MedicalLexer:
    global _LEXER
    with _LEXER_LOCK:
        if _LEXER is None:
            _LEXER = MedicalLexer()
        return _LEXER


if __name__ == "__main__":
    demo = ("Осмотр невролога (ф. 025/у). М, 36 г. Код МКБ: F43.2. "
            "Жал.: трев., инсомния. Лаб: креат. 86 мкмоль/л, СКФ 92, КФК 190, ТТГ 1.8. "
            "Принимает метопролол и флуконазол. Шкалы: ГТР-7 12б, PHQ9 7б, C-SSRS=0.")
    for m in get_lexer().scan(demo):
        print(m)
