# -*- coding: utf-8 -*-
"""
backend/bridges/bio_native.py
=============================
Строго типизированный ctypes-мост к нативным C11-модулям:
- c_core/bio_dsp_dst.c (LPC-4 глоттальный фильтр, TKEO, Вейбулл, W2-адаптация, Демпстер-Шефер)
- c_core/micronets_int8.c (W8A8 INT8 инференс микросетей + Mondrian RAPS в C11)
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
from pathlib import Path
from typing import Dict, Optional


from backend.bridges.loader import resolve_bridge_lib


class CAcousticMetrics(ctypes.Structure):
    _fields_ = [
        ("f0_mean_hz", ctypes.c_double),
        ("f0_std_hz", ctypes.c_double),
        ("jitter_ppq5_pct", ctypes.c_double),
        ("shimmer_apq5_pct", ctypes.c_double),
        ("glottal_tkeo_instability", ctypes.c_double),
        ("glottal_tkeo_kurtosis", ctypes.c_double),
        ("weibull_pause_shape_k", ctypes.c_double),
        ("weibull_pause_scale_lambda_ms", ctypes.c_double),
        ("cognitive_pause_fraction", ctypes.c_double),
        ("ru_plosive_closure_fraction", ctypes.c_double),
        ("lpc_spectral_tilt_db", ctypes.c_double),
        ("hnr_db", ctypes.c_double),
        ("snr_db", ctypes.c_double),
        ("ru_phonetic_shift_mahalanobis", ctypes.c_double),
        ("z_score_depression_invariant", ctypes.c_double),
        ("z_score_anxiety_invariant", ctypes.c_double),
    ]

    def to_dict(self) -> Dict[str, float]:
        return {field[0]: round(float(getattr(self, field[0])), 6) for field in self._fields_}


class CCrossLingualCalib(ctypes.Structure):
    _fields_ = [
        ("mu_en_dep", ctypes.c_double),
        ("sigma_en_dep", ctypes.c_double),
        ("mu_ru_dep", ctypes.c_double),
        ("sigma_ru_dep", ctypes.c_double),
        ("mu_en_anx", ctypes.c_double),
        ("sigma_en_anx", ctypes.c_double),
        ("mu_ru_anx", ctypes.c_double),
        ("sigma_ru_anx", ctypes.c_double),
        ("shafer_discount_gamma", ctypes.c_double),
    ]


class CEvidentialResult(ctypes.Structure):
    _fields_ = [
        ("bpa_ehr", ctypes.c_double * 8),
        ("bpa_dam_disc", ctypes.c_double * 8),
        ("bpa_fused", ctypes.c_double * 8),
        ("belief", ctypes.c_double * 3),
        ("plausibility", ctypes.c_double * 3),
        ("pignistic_prob", ctypes.c_double * 3),
        ("dempster_conflict_k", ctypes.c_double),
        ("shafer_reliability_beta", ctypes.c_double),
        ("dissimulation_index", ctypes.c_double),
        ("aggravation_index", ctypes.c_double),
        ("epistemic_uncertainty", ctypes.c_double),
        ("dominant_singleton_idx", ctypes.c_int32),
        ("phenotype_flag_code", ctypes.c_int32),
        ("recommended_tier", ctypes.c_int32),
    ]


class CMicroNetConformalOut(ctypes.Structure):
    _fields_ = [
        ("probabilities", ctypes.c_float * 3),
        ("raps_scores", ctypes.c_float * 3),
        ("point_confidence", ctypes.c_float),
        ("shannon_entropy_nats", ctypes.c_float),
        ("point_prediction_idx", ctypes.c_int32),
        ("prediction_set_bitmask", ctypes.c_uint32),
        ("prediction_set_size", ctypes.c_int32),
        ("is_ambiguous_or_ood", ctypes.c_int32),
        ("mandatory_escalation", ctypes.c_int32),
    ]


class CMicroNetDescriptor(ctypes.Structure):
    _fields_ = [
        ("hidden_dim", ctypes.c_int32),
        ("num_blocks", ctypes.c_int32),
        ("input_mean", ctypes.c_float * 24),
        ("input_std", ctypes.c_float * 24),
        ("stem_w_int8", ctypes.POINTER(ctypes.c_int8)),
        ("stem_scale", ctypes.POINTER(ctypes.c_float)),
        ("stem_bias", ctypes.POINTER(ctypes.c_float)),
        ("block_fc1_w_int8", ctypes.POINTER(ctypes.c_int8) * 4),
        ("block_fc1_scale", ctypes.POINTER(ctypes.c_float) * 4),
        ("block_fc1_bias", ctypes.POINTER(ctypes.c_float) * 4),
        ("block_fc2_w_int8", ctypes.POINTER(ctypes.c_int8) * 4),
        ("block_fc2_scale", ctypes.POINTER(ctypes.c_float) * 4),
        ("block_fc2_bias", ctypes.POINTER(ctypes.c_float) * 4),
        ("head_w_int8", ctypes.POINTER(ctypes.c_int8)),
        ("head_scale", ctypes.POINTER(ctypes.c_float)),
        ("head_bias", ctypes.POINTER(ctypes.c_float)),
        ("temperature", ctypes.c_float),
        ("lambda_reg", ctypes.c_float),
        ("k_reg", ctypes.c_int32),
        ("class_quantiles", ctypes.c_float * 3),
        ("critical_class_bitmask", ctypes.c_uint32),
    ]


def ensure_c11_bio_mnets_compiled() -> Path:
    """
    Компилирует c_core/bio_dsp_dst.c и c_core/micronets_int8.c в разделяемую библиотеку
    libcompass_bio_mnets.(so|dylib) с флагами -O3 -std=c11 -fPIC -lm.
    """
    repo_root = Path(__file__).resolve().parent.parent
    c_dir = repo_root / "c_core"
    ext = "dylib" if platform.system() == "Darwin" else "so"
    lib_path = c_dir / f"libcompass_bio_mnets.{ext}"

    src_files = [
        c_dir / "bio_dsp_dst.c",
        c_dir / "micronets_int8.c",
    ]
    hdr_files = [
        c_dir / "bio_dsp_dst.h",
        c_dir / "micronets_int8.h",
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


class NativeBioMnetsLib:
    """Синглтон-обертка над скомпилированной C11 библиотекой."""
    _instance: Optional["NativeBioMnetsLib"] = None

    def __init__(self) -> None:
# Приоритет: общий libcompass_core.so (build_c_core собирает все c_core/*.c разом);
# компилятор в рантайме не нужен — multi-stage-образ собирается без gcc.
        lib_path = str(resolve_bridge_lib("libcompass_bio_mnets", ('compass_micronet_forward_raps', 'compass_raps_evaluate_logits3', 'compass_w8a8_gemv', 'compass_dempster_shafer_bio_fuse', 'compass_fit_weibull_russian_pauses', 'compass_analyze_pcm_russian_invariant')))
        self.lib = ctypes.CDLL(str(lib_path))
        self._bind_signatures()

    @classmethod
    def get(cls) -> "NativeBioMnetsLib":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _bind_signatures(self) -> None:
        self.lib.compass_analyze_pcm_russian_invariant.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_size_t,
            ctypes.c_int32,
            ctypes.POINTER(CAcousticMetrics),
        ]
        self.lib.compass_analyze_pcm_russian_invariant.restype = ctypes.c_int32

        self.lib.compass_fit_weibull_russian_pauses.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        self.lib.compass_fit_weibull_russian_pauses.restype = None

        self.lib.compass_dempster_shafer_bio_fuse.argtypes = [
            ctypes.POINTER(CAcousticMetrics),
            ctypes.POINTER(CCrossLingualCalib),
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_int32,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.POINTER(CEvidentialResult),
        ]
        self.lib.compass_dempster_shafer_bio_fuse.restype = ctypes.c_int32

        self.lib.compass_w8a8_gemv.argtypes = [
            ctypes.POINTER(ctypes.c_int8),
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_float),
        ]
        self.lib.compass_w8a8_gemv.restype = None

        self.lib.compass_micronet_forward_raps.argtypes = [
            ctypes.POINTER(CMicroNetDescriptor),
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(CMicroNetConformalOut),
        ]
        self.lib.compass_micronet_forward_raps.restype = ctypes.c_int32

        self.lib.compass_raps_evaluate_logits3.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_uint32,
            ctypes.POINTER(CMicroNetConformalOut),
        ]
        self.lib.compass_raps_evaluate_logits3.restype = None