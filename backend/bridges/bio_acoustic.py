# -*- coding: utf-8 -*-
"""
backend/bridges/bio_acoustic.py
===========================
Высокоуровневый интерфейс к нативному C11 DSP-ядру (c_core/bio_dsp_dst.c)
с инверсной LPC-4 фильтрацией русского речевого тракта, оператором энергии
Тигера-Кайзера на глоттальном остатке и бимодальным MLE Вейбулла.
"""

from __future__ import annotations

import ctypes
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

import numpy as np

from backend.bridges.bio_native import CAcousticMetrics, NativeBioMnetsLib
from backend.paths import CLINICAL_DATA_DIR


@dataclass(frozen=True)
class AcousticBiomarkerVector:
    """Языково-инвариантный вектор акустических биомаркеров, рассчитанный в C11."""
    f0_mean_hz: float
    f0_std_hz: float
    jitter_ppq5_pct: float
    shimmer_apq5_pct: float
    glottal_tkeo_instability: float
    glottal_tkeo_kurtosis: float
    weibull_pause_shape_k: float
    weibull_pause_scale_lambda_ms: float
    cognitive_pause_fraction: float
    ru_plosive_closure_fraction: float
    lpc_spectral_tilt_db: float
    hnr_db: float
    snr_db: float
    ru_phonetic_shift_mahalanobis: float
    z_score_depression_invariant: float
    z_score_anxiety_invariant: float

    def to_dict(self) -> Dict[str, float]:
        return {k: round(float(v), 6) for k, v in asdict(self).items()}


class NonlinearAcousticDSP:
    """Обертка над C11 LPC-4 + Glottal TKEO + Russian Weibull MLE."""

    def __init__(self, calibration_path: Optional[Union[str, Path]] = None) -> None:
        self._clib = NativeBioMnetsLib.get().lib
        self.calibration_data = self._load_calibration(calibration_path)

    @staticmethod
    def _load_calibration(calibration_path: Optional[Union[str, Path]]) -> Dict:
        if calibration_path is None:
            calibration_path = CLINICAL_DATA_DIR / "kintsugi_dam_offline_calibration.json"
        path_obj = Path(calibration_path)
        if path_obj.exists():
            try:
                with path_obj.open("r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return {}
        return {}

    def extract_from_pcm(
        self,
        pcm_signal: np.ndarray,
        sample_rate: int = 16000,
    ) -> AcousticBiomarkerVector:
        """Вызывает C11-функцию compass_analyze_pcm_russian_invariant."""
        x = np.ascontiguousarray(np.asarray(pcm_signal, dtype=np.float64).ravel())
        c_metrics = CAcousticMetrics()
        ptr = x.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        rc = self._clib.compass_analyze_pcm_russian_invariant(
            ptr, ctypes.c_size_t(x.size), ctypes.c_int32(sample_rate), ctypes.byref(c_metrics)
        )
        if rc != 0:
            raise ValueError(f"C11 DSP engine returned error code {rc} (buffer too short or silent).")

        return AcousticBiomarkerVector(**c_metrics.to_dict())

    def fit_weibull_pause_mle(self, pause_durations_ms: np.ndarray) -> Tuple[float, float, float]:
        """Вызывает C11-решатель Ньютона-Рафсона с бимодальным фильтром русских смычек."""
        arr = np.ascontiguousarray(np.asarray(pause_durations_ms, dtype=np.float64).ravel())
        out_k = ctypes.c_double(0.0)
        out_lam = ctypes.c_double(0.0)
        out_plosive = ctypes.c_double(0.0)
        ptr = arr.ctypes.data_as(ctypes.POINTER(ctypes.c_double)) if arr.size > 0 else None
        self._clib.compass_fit_weibull_russian_pauses(
            ptr,
            ctypes.c_size_t(arr.size),
            ctypes.byref(out_k),
            ctypes.byref(out_lam),
            ctypes.byref(out_plosive),
        )
        return float(out_k.value), float(out_lam.value), float(out_plosive.value)

    def build_from_telemetry_summary(self, telemetry: Dict[str, float]) -> AcousticBiomarkerVector:
        """Конструирует структуру биомаркеров из предрасчитанной телеметрии."""
        f0_mean = float(telemetry.get("f0_mean_hz", 136.0))
        f0_std = float(telemetry.get("f0_std_hz", 15.0))
        jitter = float(telemetry.get("jitter_ppq5_pct", 0.48))
        shimmer = float(telemetry.get("shimmer_apq5_pct", 2.25))
        tkeo_inst = float(telemetry.get("glottal_tkeo_instability", telemetry.get("tkeo_instability_index", 0.20)))
        tkeo_kurt = float(telemetry.get("glottal_tkeo_kurtosis", telemetry.get("tkeo_kurtosis", 1.80)))
        weibull_k = float(telemetry.get("weibull_pause_shape_k", 1.20))
        weibull_lam = float(telemetry.get("weibull_pause_scale_lambda_ms", 330.0))
        cog_pause = float(telemetry.get("cognitive_pause_fraction", telemetry.get("pause_fraction", 0.18)))
        plosive_frac = float(telemetry.get("ru_plosive_closure_fraction", 0.26))
        tilt_db = float(telemetry.get("lpc_spectral_tilt_db", 7.6))
        hnr_db = float(telemetry.get("hnr_db", 17.8))
        snr_db = float(telemetry.get("snr_db", telemetry.get("signal_quality_snr_db", 28.0)))

        z_plosive = (plosive_frac - 0.28) / 0.14
        z_tilt = (tilt_db - 7.5) / 2.8
        mahal_phon = float(np.sqrt(0.5 * (z_plosive ** 2 + z_tilt ** 2)))

        z_w_tail = (1.25 - weibull_k) / 0.28
        z_w_scale = (weibull_lam - 320.0) / 115.0
        z_p_frac = (cog_pause - 0.16) / 0.065
        z_ru_mono = max(0.0, (16.5 - f0_std) / 5.5)
        z_dep = float(np.clip(0.34 * z_w_tail + 0.26 * z_w_scale + 0.24 * z_p_frac + 0.16 * z_ru_mono, -3.5, 5.5))

        z_tkeo = (tkeo_inst - 0.18) / 0.075
        z_jit = (jitter - 0.45) / 0.22
        z_shim = (shimmer - 2.10) / 0.85
        z_anx = float(np.clip(0.44 * z_tkeo + 0.32 * z_jit + 0.24 * z_shim, -3.5, 5.5))

        return AcousticBiomarkerVector(
            f0_mean_hz=f0_mean,
            f0_std_hz=f0_std,
            jitter_ppq5_pct=jitter,
            shimmer_apq5_pct=shimmer,
            glottal_tkeo_instability=tkeo_inst,
            glottal_tkeo_kurtosis=tkeo_kurt,
            weibull_pause_shape_k=weibull_k,
            weibull_pause_scale_lambda_ms=weibull_lam,
            cognitive_pause_fraction=cog_pause,
            ru_plosive_closure_fraction=plosive_frac,
            lpc_spectral_tilt_db=tilt_db,
            hnr_db=hnr_db,
            snr_db=snr_db,
            ru_phonetic_shift_mahalanobis=mahal_phon,
            z_score_depression_invariant=z_dep,
            z_score_anxiety_invariant=z_anx,
        )