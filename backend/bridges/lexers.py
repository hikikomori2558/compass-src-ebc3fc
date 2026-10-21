# -*- coding: utf-8 -*-
"""
backend/bridges/lexers.py — единый модуль C-лексеров ЭМК + PK-решателя
=====================================================================
Два независимых движка с непересекающимися зонами ответственности:

1. Ахо—Корасик (c_core/simd_lexer.c) — ТОЧНЫЙ многопаттерновый поиск по словарю:
   MedicalLexer/get_lexer() отдаёт кандидатов (icd10/препараты/шкалы/аббревиатуры,
   позиции в байтах) за один проход; валидацию контекста делает парсер ЭМК
   (backend/services/ehr_extract.py).
2. Bitap/Wu-Manber k<=2 (c_core/bitap_ru_lexer.c) — НЕЧЁТКИЙ клинический NER
   (опечатки, нормализация русского UTF-8, детекция отрицаний) и рядом с ним
   PK-решатель RK45(5)4 (c_core/pk_rk45_solver.c, CYP2D6/3A4, Михаэлис-Ментен):
   C11PopPKAndBitapEngine.
============================
Строго типизированный ctypes-мост к нативным C11-модулям Варианта 3:
1. c_core/pk_rk45_solver.c (Решатель ОДУ Дорманда-Принса RK45(5)4 для двухкомпартментной
   фармакокинетики Авиандра с нелинейным клиренсом Михаэлиса-Ментен CYP2D6/3A4).
2. c_core/bitap_ru_lexer.c (Нечеткий 64-битный клинический NER-лексер Wu-Manber / Bitap
   с однобайтовой канонической нормализацией русского UTF-8 и детекцией отрицаний).
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional


from backend.bridges.loader import load_core_lib, resolve_bridge_lib


class CPopPKParams(ctypes.Structure):
    _fields_ = [
        ("dose_mg", ctypes.c_double),
        ("dosing_interval_hours", ctypes.c_double),
        ("total_doses_count", ctypes.c_int32),
        ("bioavailability_f", ctypes.c_double),
        ("ka_per_h", ctypes.c_double),
        ("v_central_l", ctypes.c_double),
        ("v_periph_l", ctypes.c_double),
        ("q_intercomp_l_h", ctypes.c_double),
        ("vmax_cyp2d6_mg_h", ctypes.c_double),
        ("km_cyp2d6_ng_ml", ctypes.c_double),
        ("vmax_cyp3a4_mg_h", ctypes.c_double),
        ("km_cyp3a4_ng_ml", ctypes.c_double),
        ("cyp2d6_activity_score", ctypes.c_double),
        ("cyp2d6_inhibitor_ratio_sum", ctypes.c_double),
        ("cyp2d6_mbi_remaining_frac", ctypes.c_double),
        ("cyp3a4_inhibitor_ratio_sum", ctypes.c_double),
        ("cyp3a4_mbi_remaining_frac", ctypes.c_double),
        ("hepatic_child_pugh_factor", ctypes.c_double),
        ("egfr_ml_min", ctypes.c_double),
        ("cl_renal_baseline_l_h", ctypes.c_double),
        ("c_therapeutic_min_ng_ml", ctypes.c_double),
        ("c_therapeutic_max_ng_ml", ctypes.c_double),
        ("c_toxic_threshold_ng_ml", ctypes.c_double),
    ]


class CPopPKSolution(ctypes.Structure):
    _fields_ = [
        ("conc_plasma_ng_ml", ctypes.c_double * 169),
        ("amount_gut_mg", ctypes.c_double * 169),
        ("amount_periph_mg", ctypes.c_double * 169),
        ("c_max_ng_ml", ctypes.c_double),
        ("t_max_hours", ctypes.c_double),
        ("c_trough_ss_ng_ml", ctypes.c_double),
        ("auc_0_168_ng_h_ml", ctypes.c_double),
        ("effective_half_life_h", ctypes.c_double),
        ("accumulation_index_rac", ctypes.c_double),
        ("mass_balance_error_mg", ctypes.c_double),
        ("rk45_accepted_steps", ctypes.c_int32),
        ("rk45_rejected_steps", ctypes.c_int32),
        ("risk_classification_code", ctypes.c_int32),
        ("recommended_dose_adjustment_factor", ctypes.c_double),
    ]


class CBitapPattern(ctypes.Structure):
    _fields_ = [
        ("rule_id", ctypes.c_int32),
        ("category_code", ctypes.c_int32),
        ("pattern_len", ctypes.c_int32),
        ("max_allowed_errors", ctypes.c_int32),
        ("char_masks", ctypes.c_uint64 * 256),
        ("canonical_name", ctypes.c_char * 64),
        ("ddi_ki_ratio_impact", ctypes.c_double),
    ]


class CBitapDictionary(ctypes.Structure):
    _fields_ = [
        ("num_patterns", ctypes.c_int32),
        ("patterns", CBitapPattern * 64),
    ]


class CLexMatch(ctypes.Structure):
    _fields_ = [
        ("rule_id", ctypes.c_int32),
        ("category_code", ctypes.c_int32),
        ("edit_distance", ctypes.c_int32),
        ("is_negated", ctypes.c_int32),
        ("utf8_byte_start", ctypes.c_uint32),
        ("utf8_byte_end", ctypes.c_uint32),
        ("ddi_ki_ratio_impact", ctypes.c_double),
        ("canonical_name", ctypes.c_char * 64),
    ]


def ensure_c11_pk_lexer_compiled() -> Path:
    """
    Компилирует c_core/pk_rk45_solver.c и c_core/bitap_ru_lexer.c в разделяемую
    библиотеку libcompass_pk_lexer.(so|dylib) с флагами -O3 -std=c11 -fPIC -lm.
    """
    repo_root = Path(__file__).resolve().parent.parent
    c_dir = repo_root / "c_core"
    ext = "dylib" if platform.system() == "Darwin" else "so"
    lib_path = c_dir / f"libcompass_pk_lexer.{ext}"

    src_files = [
        c_dir / "pk_rk45_solver.c",
        c_dir / "bitap_ru_lexer.c",
    ]
    hdr_files = [
        c_dir / "pk_rk45_solver.h",
        c_dir / "bitap_ru_lexer.h",
    ]

    need_build = not lib_path.exists()
    if not need_build:
        lib_mtime = lib_path.stat().st_mtime
        for f in src_files + hdr_files:
            if f.exists() and f.stat().st_mtime > lib_mtime:
                need_build = True
                break

    if need_build:
        cmd = [
            "cc",
            "-O3",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-fPIC",
            "-shared",
            str(src_files[0]),
            str(src_files[1]),
            "-o",
            str(lib_path),
            "-lm",
        ]
        subprocess.run(cmd, check=True)

    return lib_path


class NativePKLexerLib:
    """Синглтон-обертка над скомпилированной C11 библиотекой libcompass_pk_lexer."""
    _instance: Optional["NativePKLexerLib"] = None

    def __init__(self) -> None:
# Приоритет: общий libcompass_core.so (build_c_core собирает все c_core/*.c разом);
# компилятор в рантайме не нужен — multi-stage-образ собирается без gcc.
        lib_path = str(resolve_bridge_lib("libcompass_pk_lexer", ('compass_solve_poppk_rk45', 'compass_pk_init_default_params', 'compass_bitap_init_clinical_dictionary', 'compass_bitap_add_pattern', 'compass_bitap_scan_utf8_ehr')))
        self.lib = ctypes.CDLL(str(lib_path))
        self._bind_signatures()

    @classmethod
    def get(cls) -> "NativePKLexerLib":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _bind_signatures(self) -> None:
        self.lib.compass_pk_init_default_params.argtypes = [ctypes.POINTER(CPopPKParams)]
        self.lib.compass_pk_init_default_params.restype = None

        self.lib.compass_solve_poppk_rk45.argtypes = [
            ctypes.POINTER(CPopPKParams),
            ctypes.POINTER(CPopPKSolution),
        ]
        self.lib.compass_solve_poppk_rk45.restype = ctypes.c_int32

        self.lib.compass_bitap_init_clinical_dictionary.argtypes = [
            ctypes.POINTER(CBitapDictionary)
        ]
        self.lib.compass_bitap_init_clinical_dictionary.restype = None

        self.lib.compass_bitap_add_pattern.argtypes = [
            ctypes.POINTER(CBitapDictionary),
            ctypes.c_char_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_double,
            ctypes.c_char_p,
        ]
        self.lib.compass_bitap_add_pattern.restype = ctypes.c_int32

        self.lib.compass_bitap_scan_utf8_ehr.argtypes = [
            ctypes.POINTER(CBitapDictionary),
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(CLexMatch),
            ctypes.c_int32,
        ]
        self.lib.compass_bitap_scan_utf8_ehr.restype = ctypes.c_int32


_PK_RISK_LABELS: Dict[int, str] = {
    0: "OPTIMAL_THERAPEUTIC_WINDOW",
    1: "SUBTHERAPEUTIC_UNDEREXPOSURE",
    2: "SUPRATHERAPEUTIC_OVEREXPOSURE_WARNING",
    3: "TOXIC_OVERDOSE_HARD_STOP",
}


@dataclass(frozen=True)
class PopPKTrajectoryResult:
    """Высокоуровневый результат расчета 168-часовой траектории ОДУ RK45."""
    conc_plasma_168h_ng_ml: List[float]
    c_max_ng_ml: float
    t_max_hours: float
    c_trough_ss_ng_ml: float
    auc_0_168_ng_h_ml: float
    effective_half_life_h: float
    accumulation_index_rac: float
    mass_balance_error_mg: float
    rk45_accepted_steps: int
    rk45_rejected_steps: int
    risk_classification: str
    recommended_dose_adjustment_factor: float

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class ClinicalEntityMatch:
    """Найденная в русскоязычном тексте ЭМК клиническая сущность."""
    rule_id: int
    category_code: int
    edit_distance: int
    is_negated: bool
    matched_surface_text: str
    canonical_name: str
    ddi_ki_ratio_impact: float
    utf8_byte_span: Tuple[int, int]

    def to_dict(self) -> Dict:
        return asdict(self)


class C11PopPKAndBitapEngine:
    """
    Объединенный движок Варианта 3:
    1) Сканирует русскоязычный текст ЭМК нечетким лексером Wu-Manber/Bitap,
       находит сопутствующие ингибиторы CYP2D6/CYP3A4 и противопоказания (с учетом отрицаний).
    2) Передает найденные коэффициенты ингибирования [I]/K_i напрямую в C11-решатель
       ОДУ Дорманда-Принса RK45 и строит индивидуальную 168-часовую кривую концентрации.
    """

    def __init__(self) -> None:
        self._clib = NativePKLexerLib.get().lib
        self._dict = CBitapDictionary()
        self._clib.compass_bitap_init_clinical_dictionary(ctypes.byref(self._dict))

    def add_custom_pattern(
        self,
        utf8_pattern: str,
        rule_id: int,
        category_code: int,
        max_errors: int = 2,
        ddi_impact: float = 0.0,
        canonical_name: Optional[str] = None,
    ) -> None:
        pat_bytes = utf8_pattern.encode("utf-8")
        canon_bytes = (canonical_name or utf8_pattern).encode("utf-8")
        rc = self._clib.compass_bitap_add_pattern(
            ctypes.byref(self._dict),
            pat_bytes,
            ctypes.c_int32(rule_id),
            ctypes.c_int32(category_code),
            ctypes.c_int32(max_errors),
            ctypes.c_double(ddi_impact),
            canon_bytes,
        )
        if rc != 0:
            raise ValueError(f"compass_bitap_add_pattern failed with code {rc}")

    def scan_ehr_text(self, text: str, max_matches: int = 64) -> List[ClinicalEntityMatch]:
        raw_bytes = text.encode("utf-8")
        matches_arr = (CLexMatch * max_matches)()
        count = self._clib.compass_bitap_scan_utf8_ehr(
            ctypes.byref(self._dict),
            raw_bytes,
            ctypes.c_size_t(len(raw_bytes)),
            matches_arr,
            ctypes.c_int32(max_matches),
        )
        results: List[ClinicalEntityMatch] = []
        for i in range(int(count)):
            m = matches_arr[i]
            b_start = int(m.utf8_byte_start)
            b_end = int(m.utf8_byte_end)
            surface = raw_bytes[b_start:b_end].decode("utf-8", errors="replace")
            canon = m.canonical_name.decode("utf-8", errors="replace")
            results.append(
                ClinicalEntityMatch(
                    rule_id=int(m.rule_id),
                    category_code=int(m.category_code),
                    edit_distance=int(m.edit_distance),
                    is_negated=bool(m.is_negated),
                    matched_surface_text=surface,
                    canonical_name=canon,
                    ddi_ki_ratio_impact=round(float(m.ddi_ki_ratio_impact), 4),
                    utf8_byte_span=(b_start, b_end),
                )
            )
        return results

    def simulate_poppk_rk45(
        self,
        dose_mg: float = 20.0,
        dosing_interval_hours: float = 12.0,
        total_doses_count: int = 14,
        cyp2d6_activity_score: float = 1.0,
        cyp2d6_inhibitor_ratio_sum: float = 0.0,
        cyp2d6_mbi_remaining_frac: float = 1.0,
        cyp3a4_inhibitor_ratio_sum: float = 0.0,
        cyp3a4_mbi_remaining_frac: float = 1.0,
        hepatic_child_pugh_factor: float = 1.0,
        egfr_ml_min: float = 90.0,
    ) -> PopPKTrajectoryResult:
        p = CPopPKParams()
        self._clib.compass_pk_init_default_params(ctypes.byref(p))

        p.dose_mg = float(dose_mg)
        p.dosing_interval_hours = float(dosing_interval_hours)
        p.total_doses_count = int(total_doses_count)
        p.cyp2d6_activity_score = float(cyp2d6_activity_score)
        p.cyp2d6_inhibitor_ratio_sum = float(cyp2d6_inhibitor_ratio_sum)
        p.cyp2d6_mbi_remaining_frac = float(cyp2d6_mbi_remaining_frac)
        p.cyp3a4_inhibitor_ratio_sum = float(cyp3a4_inhibitor_ratio_sum)
        p.cyp3a4_mbi_remaining_frac = float(cyp3a4_mbi_remaining_frac)
        p.hepatic_child_pugh_factor = float(hepatic_child_pugh_factor)
        p.egfr_ml_min = float(egfr_ml_min)

        sol = CPopPKSolution()
        rc = self._clib.compass_solve_poppk_rk45(ctypes.byref(p), ctypes.byref(sol))
        if rc != 0:
            raise RuntimeError(f"C11 compass_solve_poppk_rk45 failed with code {rc}")

        code = int(sol.risk_classification_code)
        return PopPKTrajectoryResult(
            conc_plasma_168h_ng_ml=[round(float(sol.conc_plasma_ng_ml[h]), 4) for h in range(169)],
            c_max_ng_ml=round(float(sol.c_max_ng_ml), 4),
            t_max_hours=round(float(sol.t_max_hours), 2),
            c_trough_ss_ng_ml=round(float(sol.c_trough_ss_ng_ml), 4),
            auc_0_168_ng_h_ml=round(float(sol.auc_0_168_ng_h_ml), 2),
            effective_half_life_h=round(float(sol.effective_half_life_h), 2),
            accumulation_index_rac=round(float(sol.accumulation_index_rac), 4),
            mass_balance_error_mg=float(sol.mass_balance_error_mg),
            rk45_accepted_steps=int(sol.rk45_accepted_steps),
            rk45_rejected_steps=int(sol.rk45_rejected_steps),
            risk_classification=_PK_RISK_LABELS.get(code, "OPTIMAL_THERAPEUTIC_WINDOW"),
            recommended_dose_adjustment_factor=round(float(sol.recommended_dose_adjustment_factor), 4),
        )

    def analyze_ehr_and_simulate_pk(
        self,
        ehr_text: str,
        cyp2d6_activity_score: float = 1.0,
        hepatic_child_pugh_factor: float = 1.0,
        egfr_ml_min: float = 90.0,
    ) -> Dict:
        """
        Сквозной конвейер: извлекает нечеткие упоминания ко-медикаментов из текста ЭМК,
        отбрасывает отрицаемые сущности (`is_negated == True`) и автоматически
        моделирует 168-часовую фармакокинетическую кривую пациента в C11.
        """
        entities = self.scan_ehr_text(ehr_text)
        active_entities = [e for e in entities if not e.is_negated]

        cyp2d6_inh_sum = sum(
            e.ddi_ki_ratio_impact for e in active_entities if e.category_code == 3 and e.ddi_ki_ratio_impact > 0
        )
        cyp3a4_inh_sum = sum(
            e.ddi_ki_ratio_impact for e in active_entities if e.category_code == 4 and e.ddi_ki_ratio_impact > 0
        )
        # Если среди активных назначений найден пароксетин (rule_id=302), учитываем MBI-инактивацию CYP2D6
        mbi_2d6 = 0.35 if any(e.rule_id == 302 for e in active_entities) else 1.0

        pk_result = self.simulate_poppk_rk45(
            cyp2d6_activity_score=cyp2d6_activity_score,
            cyp2d6_inhibitor_ratio_sum=cyp2d6_inhibitor_ratio_sum if False else cyp2d6_inh_sum,
            cyp2d6_mbi_remaining_frac=mbi_2d6,
            cyp3a4_inhibitor_ratio_sum=cyp3a4_inh_sum,
            hepatic_child_pugh_factor=hepatic_child_pugh_factor,
            egfr_ml_min=egfr_ml_min,
        )
        hard_stops = [e.canonical_name for e in active_entities if e.category_code == 2]

        return {
            "extracted_entities": [e.to_dict() for e in entities],
            "active_contraindications": hard_stops,
            "poppk_rk45_simulation": pk_result.to_dict(),
        }

# =============================================================================
# Раздел 1. Ахо—Корасик (c_core/simd_lexer.c): точная словарная предразметка ЭМК.
# Лексер отдаёт КАНДИДАТОВ (сущность + позиция в байтах lower-версии текста) за один
# проход; клиническую валидацию контекста делает парсер ЭМК (services/ehr_extract.py).
# Конвейер: сырой текст -> .lower() -> lexer_scan -> кандидаты -> парсер.
# =============================================================================
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
