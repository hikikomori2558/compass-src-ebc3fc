/*
 * c_core/bio_dsp_dst.c
 * ====================
 * Реализация нативного C11 DSP-контура (LPC-4 глоттальная фильтрация + TKEO + Weibull)
 * и эвиденциального автомата Демпстера-Шефера с W2 кросс-лингвистической адаптацией.
 */

#include "bio_dsp_dst.h"
#include <math.h>
#include <string.h>

#define COMPASS_EPS 1e-12
#define COMPASS_PI  3.14159265358979323846
#define LPC_ORDER   4

static inline double clamp_f64(double val, double lo, double hi) {
    return (val < lo) ? lo : ((val > hi) ? hi : val);
}

static inline int32_t popcount_u32(uint32_t x) {
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_popcount(x);
#else
    int32_t count = 0;
    while (x) {
        count += (int32_t)(x & 1u);
        x >>= 1u;
    }
    return count;
#endif
}

/*
 * Рекурсия Левинсона-Дарбина 4-го порядка для оценки огибающей речевого тракта.
 * Возвращает спектральный наклон (Spectral Tilt, дБ) и заполняет коэффициенты lpc_a[1..4].
 */
static double levinson_durbin_lpc4(
    const double *frame,
    size_t len,
    double lpc_a[LPC_ORDER + 1]
) {
    double r[LPC_ORDER + 1];
    for (int m = 0; m <= LPC_ORDER; ++m) {
        double sum = 0.0;
        for (size_t n = (size_t)m; n < len; ++n) {
            sum += frame[n] * frame[n - (size_t)m];
        }
        r[m] = sum;
    }

    memset(lpc_a, 0, sizeof(double) * (LPC_ORDER + 1));
    lpc_a[0] = 1.0;

    if (r[0] <= COMPASS_EPS) {
        return 0.0;
    }

    double err = r[0];
    double a_prev[LPC_ORDER + 1];
    memset(a_prev, 0, sizeof(a_prev));

    for (int m = 1; m <= LPC_ORDER; ++m) {
        double num = r[m];
        for (int j = 1; j < m; ++j) {
            num -= lpc_a[j] * r[m - j];
        }
        double kappa = clamp_f64(num / (err + COMPASS_EPS), -0.985, 0.985);

        memcpy(a_prev, lpc_a, sizeof(double) * (LPC_ORDER + 1));
        lpc_a[m] = kappa;
        for (int j = 1; j < m; ++j) {
            lpc_a[j] = a_prev[j] - kappa * a_prev[m - j];
        }
        err *= (1.0 - kappa * kappa);
        if (err <= COMPASS_EPS) {
            break;
        }
    }

    double r1_norm = clamp_f64(r[1] / (r[0] + COMPASS_EPS), -0.99, 0.99);
    return 10.0 * log10((1.0 + fabs(r1_norm)) / (1.0 - fabs(r1_norm) + COMPASS_EPS));
}

void compass_fit_weibull_russian_pauses(
    const double *pause_durations_ms,
    size_t num_pauses,
    double *out_shape_k,
    double *out_scale_lambda_ms,
    double *out_plosive_ratio
) {
    if (!pause_durations_ms || num_pauses == 0) {
        *out_shape_k = 1.25;
        *out_scale_lambda_ms = 310.0;
        *out_plosive_ratio = 0.0;
        return;
    }

    /* Бимодальная сегрегация:
     * [15.0 .. 105.0] мс — артикуляционные смычки русских глухих согласных (п, т, к, ц, ч)
     * > 105.0 мс — истинные когнитивные/психомоторные паузы хезитации
     */
    double cog_pauses[2048];
    double log_t[2048];
    size_t n_cog = 0;
    size_t n_plosive = 0;
    size_t n_valid_total = 0;

    for (size_t i = 0; i < num_pauses; ++i) {
        double d = pause_durations_ms[i];
        if (d >= 15.0 && d <= 105.0) {
            n_plosive++;
            n_valid_total++;
        } else if (d > 105.0 && n_cog < 2048) {
            cog_pauses[n_cog] = d;
            log_t[n_cog] = log(d);
            n_cog++;
            n_valid_total++;
        }
    }

    *out_plosive_ratio = (n_valid_total > 0)
        ? ((double)n_plosive / (double)n_valid_total)
        : 0.0;

    if (n_cog == 0) {
        *out_shape_k = 1.28;
        *out_scale_lambda_ms = 290.0;
        return;
    }
    if (n_cog < 3) {
        double mean_d = 0.0;
        for (size_t i = 0; i < n_cog; ++i) {
            mean_d += cog_pauses[i];
        }
        *out_shape_k = 1.15;
        *out_scale_lambda_ms = clamp_f64(mean_d / (double)n_cog, 110.0, 4000.0);
        return;
    }

    double mean_log = 0.0;
    for (size_t i = 0; i < n_cog; ++i) {
        mean_log += log_t[i];
    }
    mean_log /= (double)n_cog;

    double var_log = 0.0;
    for (size_t i = 0; i < n_cog; ++i) {
        double diff = log_t[i] - mean_log;
        var_log += diff * diff;
    }
    var_log /= (double)n_cog;
    double std_log = sqrt(var_log + COMPASS_EPS);

    if (std_log < 1e-4) {
        *out_shape_k = 2.60;
        *out_scale_lambda_ms = clamp_f64(exp(mean_log), 110.0, 4000.0);
        return;
    }

    /* Начальное приближение Менона: k_0 = pi / (sqrt(6) * std(ln(t))) */
    double k = clamp_f64(COMPASS_PI / (2.44948974278 * std_log), 0.25, 5.0);

    /* Итерации Ньютона-Рафсона по логарифмическому правдоподобию Вейбулла */
    for (int iter = 0; iter < 35; ++iter) {
        double sum_tk = 0.0;
        double sum_tk_log = 0.0;
        double sum_tk_log2 = 0.0;

        for (size_t i = 0; i < n_cog; ++i) {
            double tk = pow(cog_pauses[i], k);
            double lt = log_t[i];
            sum_tk += tk;
            sum_tk_log += tk * lt;
            sum_tk_log2 += tk * lt * lt;
        }

        if (sum_tk <= COMPASS_EPS || !isfinite(sum_tk)) {
            break;
        }

        double ratio = sum_tk_log / sum_tk;
        double g_val = ratio - (1.0 / k) - mean_log;
        double dg_dk = (sum_tk_log2 / sum_tk) - (ratio * ratio) + (1.0 / (k * k));

        if (fabs(dg_dk) <= COMPASS_EPS) {
            break;
        }

        double k_next = clamp_f64(k - (g_val / dg_dk), 0.20, 6.0);
        if (fabs(k_next - k) < 1e-6) {
            k = k_next;
            break;
        }
        k = k_next;
    }

    double sum_tk_final = 0.0;
    for (size_t i = 0; i < n_cog; ++i) {
        sum_tk_final += pow(cog_pauses[i], k);
    }
    double scale_lam = pow(sum_tk_final / (double)n_cog, 1.0 / k);

    *out_shape_k = k;
    *out_scale_lambda_ms = clamp_f64(scale_lam, 110.0, 5000.0);
}

int32_t compass_analyze_pcm_russian_invariant(
    const double *pcm_samples,
    size_t num_samples,
    int32_t sample_rate,
    CompassAcousticMetrics *out_metrics
) {
    if (!pcm_samples || !out_metrics || sample_rate <= 0 || num_samples < (size_t)(sample_rate / 4)) {
        return -1;
    }
    memset(out_metrics, 0, sizeof(CompassAcousticMetrics));

    double peak = 0.0;
    for (size_t i = 0; i < num_samples; ++i) {
        double a = fabs(pcm_samples[i]);
        if (a > peak) peak = a;
    }
    if (peak <= COMPASS_EPS) {
        return -2;
    }
    double inv_peak = 1.0 / peak;

    size_t frame_len = (size_t)((sample_rate * 25) / 1000);
    size_t hop_len   = (size_t)((sample_rate * 10) / 1000);
    if (frame_len < 64) frame_len = 64;
    if (frame_len > 1024) frame_len = 1024;
    if (hop_len < 32) hop_len = 32;

    size_t num_frames = (num_samples > frame_len)
        ? ((num_samples - frame_len) / hop_len + 1)
        : 1;
    if (num_frames > 4096) num_frames = 4096;

    double frame_buf[1024];
    double residual_buf[1024];
    double rms_frames[4096];
    double f0_list[4096];
    double periods_list[4096];
    double amps_list[4096];
    size_t voiced_count = 0;

    double tkeo_sum = 0.0;
    double tkeo_sum2 = 0.0;
    double tkeo_sum4 = 0.0;
    size_t tkeo_count = 0;

    double tilt_sum = 0.0;
    double hnr_sum = 0.0;

    size_t min_lag = (size_t)(sample_rate / 400);
    size_t max_lag = (size_t)(sample_rate / 65);
    if (min_lag < 2) min_lag = 2;
    if (max_lag >= frame_len - 2) max_lag = frame_len - 3;

    for (size_t f = 0; f < num_frames; ++f) {
        size_t offset = f * hop_len;
        double frame_energy = 0.0;
        double frame_peak_amp = 0.0;

        for (size_t i = 0; i < frame_len; ++i) {
            double s = (offset + i < num_samples) ? (pcm_samples[offset + i] * inv_peak) : 0.0;
            frame_buf[i] = s;
            frame_energy += s * s;
            double abs_s = fabs(s);
            if (abs_s > frame_peak_amp) frame_peak_amp = abs_s;
        }
        rms_frames[f] = sqrt(frame_energy / (double)frame_len + COMPASS_EPS);

        double lpc_a[LPC_ORDER + 1];
        double tilt_db = levinson_durbin_lpc4(frame_buf, frame_len, lpc_a);
        tilt_sum += tilt_db;

        for (size_t i = 0; i < frame_len; ++i) {
            double pred = 0.0;
            for (int k = 1; k <= LPC_ORDER; ++k) {
                if (i >= (size_t)k) {
                    pred += lpc_a[k] * frame_buf[i - (size_t)k];
                }
            }
            residual_buf[i] = frame_buf[i] - pred;
        }

        for (size_t i = 1; i + 1 < frame_len; ++i) {
            double psi = fabs(residual_buf[i] * residual_buf[i] - residual_buf[i - 1] * residual_buf[i + 1]);
            tkeo_sum += psi;
            double psi2 = psi * psi;
            tkeo_sum2 += psi2;
            tkeo_sum4 += psi2 * psi2;
            tkeo_count++;
        }

        if (rms_frames[f] > 0.025 && max_lag > min_lag) {
            double r0 = 0.0;
            for (size_t i = 0; i < frame_len; ++i) {
                r0 += residual_buf[i] * residual_buf[i];
            }
            if (r0 > COMPASS_EPS) {
                double best_r = -1.0;
                size_t best_lag = min_lag;
                for (size_t lag = min_lag; lag <= max_lag; lag += 2) {
                    double r_lag = 0.0;
                    for (size_t i = 0; i + lag < frame_len; ++i) {
                        r_lag += residual_buf[i] * residual_buf[i + lag];
                    }
                    double norm_r = r_lag / r0;
                    if (norm_r > best_r) {
                        best_r = norm_r;
                        best_lag = lag;
                    }
                }
                for (int delta = -1; delta <= 1; delta += 2) {
                    int cand_i = (int)best_lag + delta;
                    if (cand_i >= (int)min_lag && cand_i <= (int)max_lag) {
                        size_t cand_lag = (size_t)cand_i;
                        double r_lag = 0.0;
                        for (size_t i = 0; i + cand_lag < frame_len; ++i) {
                            r_lag += residual_buf[i] * residual_buf[i + cand_lag];
                        }
                        double norm_r = r_lag / r0;
                        if (norm_r > best_r) {
                            best_r = norm_r;
                            best_lag = cand_lag;
                        }
                    }
                }

                if (best_r > 0.28 && voiced_count < 4096) {
                    double f0 = (double)sample_rate / (double)best_lag;
                    f0_list[voiced_count] = f0;
                    periods_list[voiced_count] = 1.0 / f0;
                    amps_list[voiced_count] = frame_peak_amp;
                    double r_clip = clamp_f64(best_r, 0.05, 0.995);
                    hnr_sum += 10.0 * log10(r_clip / (1.0 - r_clip));
                    voiced_count++;
                }
            }
        }
    }

    double tkeo_mean = (tkeo_count > 0) ? (tkeo_sum / (double)tkeo_count) : 0.0;
    double tkeo_var  = (tkeo_count > 1)
        ? fmax(0.0, (tkeo_sum2 / (double)tkeo_count) - tkeo_mean * tkeo_mean)
        : 0.0;
    double tkeo_std  = sqrt(tkeo_var + COMPASS_EPS);
    double tkeo_instability = (tkeo_mean > COMPASS_EPS)
        ? clamp_f64(tkeo_std / (tkeo_mean + COMPASS_EPS) * 0.16, 0.02, 4.5)
        : 0.18;

    double m4 = (tkeo_count > 0) ? (tkeo_sum4 / (double)tkeo_count) : 0.0;
    double kurt = (tkeo_var > COMPASS_EPS)
        ? clamp_f64((m4 / (tkeo_var * tkeo_var + COMPASS_EPS)) - 3.0, -2.0, 45.0)
        : 1.5;

    double jitter_ppq5 = 0.44;
    double shimmer_apq5 = 2.10;
    if (voiced_count >= 5) {
        double p_mean = 0.0;
        double a_mean = 0.0;
        for (size_t i = 0; i < voiced_count; ++i) {
            p_mean += periods_list[i];
            a_mean += amps_list[i];
        }
        p_mean /= (double)voiced_count;
        a_mean /= (double)voiced_count;

        double diff_p_sum = 0.0;
        double diff_a_sum = 0.0;
        size_t n_quint = voiced_count - 4;
        for (size_t i = 2; i + 2 < voiced_count; ++i) {
            double local_p = 0.2 * (periods_list[i - 2] + periods_list[i - 1] + periods_list[i] + periods_list[i + 1] + periods_list[i + 2]);
            double local_a = 0.2 * (amps_list[i - 2] + amps_list[i - 1] + amps_list[i] + amps_list[i + 1] + amps_list[i + 2]);
            diff_p_sum += fabs(periods_list[i] - local_p);
            diff_a_sum += fabs(amps_list[i] - local_a);
        }
        jitter_ppq5 = clamp_f64((diff_p_sum / (double)n_quint) / (p_mean + COMPASS_EPS) * 100.0, 0.02, 12.0);
        shimmer_apq5 = clamp_f64((diff_a_sum / (double)n_quint) / (a_mean + COMPASS_EPS) * 100.0, 0.10, 30.0);
    }

    double f0_mean = 138.0;
    double f0_std = 15.5;
    if (voiced_count >= 2) {
        double s1 = 0.0;
        for (size_t i = 0; i < voiced_count; ++i) s1 += f0_list[i];
        f0_mean = s1 / (double)voiced_count;
        double s2 = 0.0;
        for (size_t i = 0; i < voiced_count; ++i) {
            double d = f0_list[i] - f0_mean;
            s2 += d * d;
        }
        f0_std = sqrt(s2 / (double)voiced_count);
    }

    double min_rms = rms_frames[0];
    double max_rms = rms_frames[0];
    for (size_t f = 1; f < num_frames; ++f) {
        if (rms_frames[f] < min_rms) min_rms = rms_frames[f];
        if (rms_frames[f] > max_rms) max_rms = rms_frames[f];
    }
    double snr_db = clamp_f64(20.0 * log10((max_rms + 1e-6) / (min_rms + 1e-6)), 5.0, 55.0);
    double vad_thresh = min_rms + 0.20 * fmax(max_rms - min_rms, 1e-5);

    double pause_runs_ms[2048];
    size_t n_runs = 0;
    size_t cur_run = 0;
    size_t cog_silent_frames = 0;
    double hop_ms = (double)hop_len * 1000.0 / (double)sample_rate;

    for (size_t f = 0; f < num_frames; ++f) {
        if (rms_frames[f] < vad_thresh) {
            cur_run++;
        } else if (cur_run > 0) {
            double dur_ms = (double)cur_run * hop_ms;
            if (n_runs < 2048) pause_runs_ms[n_runs++] = dur_ms;
            if (dur_ms > 105.0) cog_silent_frames += cur_run;
            cur_run = 0;
        }
    }
    if (cur_run > 0) {
        double dur_ms = (double)cur_run * hop_ms;
        if (n_runs < 2048) pause_runs_ms[n_runs++] = dur_ms;
        if (dur_ms > 105.0) cog_silent_frames += cur_run;
    }

    double weibull_k = 1.25;
    double weibull_lam = 310.0;
    double plosive_ratio = 0.0;
    compass_fit_weibull_russian_pauses(pause_runs_ms, n_runs, &weibull_k, &weibull_lam, &plosive_ratio);

    double cog_pause_frac = (double)cog_silent_frames / (double)num_frames;
    double mean_tilt = tilt_sum / (double)num_frames;
    double mean_hnr  = (voiced_count > 0) ? (hnr_sum / (double)voiced_count) : 17.5;

    double z_plosive = (plosive_ratio - 0.28) / 0.14;
    double z_tilt    = (mean_tilt - 7.5) / 2.8;
    double mahal_phon = sqrt(0.5 * (z_plosive * z_plosive + z_tilt * z_tilt));

    double z_weibull_tail  = (1.25 - weibull_k) / 0.28;
    double z_weibull_scale = (weibull_lam - 320.0) / 115.0;
    double z_pause_frac    = (cog_pause_frac - 0.16) / 0.065;
    double z_ru_monopitch  = fmax(0.0, (16.5 - f0_std) / 5.5);

    double z_dep_inv = clamp_f64(
        0.34 * z_weibull_tail + 0.26 * z_weibull_scale + 0.24 * z_pause_frac + 0.16 * z_ru_monopitch,
        -3.5, 5.5
    );

    double z_tkeo    = (tkeo_instability - 0.18) / 0.075;
    double z_jitter  = (jitter_ppq5 - 0.45) / 0.22;
    double z_shimmer = (shimmer_apq5 - 2.10) / 0.85;
    double z_anx_inv = clamp_f64(
        0.44 * z_tkeo + 0.32 * z_jitter + 0.24 * z_shimmer,
        -3.5, 5.5
    );

    out_metrics->f0_mean_hz                    = f0_mean;
    out_metrics->f0_std_hz                     = f0_std;
    out_metrics->jitter_ppq5_pct               = jitter_ppq5;
    out_metrics->shimmer_apq5_pct              = shimmer_apq5;
    out_metrics->glottal_tkeo_instability      = tkeo_instability;
    out_metrics->glottal_tkeo_kurtosis         = kurt;
    out_metrics->weibull_pause_shape_k         = weibull_k;
    out_metrics->weibull_pause_scale_lambda_ms = weibull_lam;
    out_metrics->cognitive_pause_fraction      = cog_pause_frac;
    out_metrics->ru_plosive_closure_fraction   = plosive_ratio;
    out_metrics->lpc_spectral_tilt_db          = mean_tilt;
    out_metrics->hnr_db                        = mean_hnr;
    out_metrics->snr_db                        = snr_db;
    out_metrics->ru_phonetic_shift_mahalanobis = mahal_phon;
    out_metrics->z_score_depression_invariant  = z_dep_inv;
    out_metrics->z_score_anxiety_invariant     = z_anx_inv;

    return 0;
}

static void normalize_bpa_from_evidence(
    double e_rem,
    double e_rel,
    double e_adv,
    double e_comp,
    double w_prior,
    double bpa_out[8]
) {
    memset(bpa_out, 0, sizeof(double) * 8);
    e_rem   = fmax(0.0, e_rem);
    e_rel   = fmax(0.0, e_rel);
    e_adv   = fmax(0.0, e_adv);
    e_comp  = fmax(0.0, e_comp);
    w_prior = fmax(0.4, w_prior);

    double total = e_rem + e_rel + e_adv + e_comp + w_prior;
    bpa_out[DST_MASK_REMISSION]     = e_rem / total;
    bpa_out[DST_MASK_RELAPSE]       = e_rel / total;
    bpa_out[DST_MASK_ADVERSE]       = e_adv / total;
    bpa_out[DST_MASK_DETERIORATION] = e_comp / total;
    bpa_out[DST_MASK_THETA]         = w_prior / total;
}

int32_t compass_dempster_shafer_bio_fuse(
    const CompassAcousticMetrics *metrics,
    const CompassCrossLingualCalib *calib,
    double raw_kintsugi_dep_score,
    double raw_kintsugi_anx_score,
    double hama_total,
    double madrs_total,
    double self_report_wellbeing,
    int32_t adverse_event_count,
    double cyp_inhibition_factor,
    double response_latency_sec,
    CompassEvidentialResult *out_result
) {
    if (!metrics || !calib || !out_result) {
        return -1;
    }
    memset(out_result, 0, sizeof(CompassEvidentialResult));

    double z_dam_dep_aligned = metrics->z_score_depression_invariant;
    double z_dam_anx_aligned = metrics->z_score_anxiety_invariant;

    if (raw_kintsugi_dep_score >= 0.0 && calib->sigma_ru_dep > COMPASS_EPS) {
        double aligned_dep = calib->mu_en_dep +
            (calib->sigma_en_dep / calib->sigma_ru_dep) * (raw_kintsugi_dep_score - calib->mu_ru_dep);
        double z_kintsugi_dep = (aligned_dep - calib->mu_en_dep) / fmax(calib->sigma_en_dep, 0.05);
        double beta_ru = exp(-fmax(0.0, calib->shafer_discount_gamma) *
                             metrics->ru_phonetic_shift_mahalanobis *
                             metrics->ru_phonetic_shift_mahalanobis);
        out_result->shafer_reliability_beta = clamp_f64(beta_ru, 0.15, 1.0);

        double w_dam = 0.55 * out_result->shafer_reliability_beta;
        z_dam_dep_aligned = w_dam * z_kintsugi_dep + (1.0 - w_dam) * metrics->z_score_depression_invariant;
    } else {
        out_result->shafer_reliability_beta = 1.0;
    }

    if (raw_kintsugi_anx_score >= 0.0 && calib->sigma_ru_anx > COMPASS_EPS) {
        double aligned_anx = calib->mu_en_anx +
            (calib->sigma_en_anx / calib->sigma_ru_anx) * (raw_kintsugi_anx_score - calib->mu_ru_anx);
        double z_kintsugi_anx = (aligned_anx - calib->mu_en_anx) / fmax(calib->sigma_en_anx, 0.05);
        double w_dam = 0.55 * out_result->shafer_reliability_beta;
        z_dam_anx_aligned = w_dam * z_kintsugi_anx + (1.0 - w_dam) * metrics->z_score_anxiety_invariant;
    }

    double hama_norm  = clamp_f64(hama_total / 30.0, 0.0, 1.8);
    double madrs_norm = clamp_f64(madrs_total / 34.0, 0.0, 1.8);
    double wellbeing  = clamp_f64(self_report_wellbeing, 0.0, 1.0);

    double e_ehr_rem  = 8.5 * wellbeing * fmax(0.0, 1.0 - 0.55 * (hama_norm + madrs_norm));
    double e_ehr_rel  = 5.5 * (0.45 * hama_norm + 0.45 * madrs_norm + 0.35 * (1.0 - wellbeing));
    double cyp_excess = fmax(0.0, cyp_inhibition_factor - 1.15);
    double e_ehr_adv  = 2.8 * (double)fmax(0, adverse_event_count) + 3.2 * cyp_excess;
    double e_ehr_comp = 1.2 * fmin(e_ehr_rel, e_ehr_adv);

    normalize_bpa_from_evidence(e_ehr_rem, e_ehr_rel, e_ehr_adv, e_ehr_comp, 2.5, out_result->bpa_ehr);

    double snr_factor = clamp_f64((metrics->snr_db - 8.0) / 22.0, 0.25, 1.0);
    double lat_pen    = fmax(0.0, (response_latency_sec - 2.8) / 2.5);
    double distress   = fmax(0.0, 0.55 * z_dam_anx_aligned + 0.55 * z_dam_dep_aligned + 0.40 * lat_pen);

    double e_dam_rem  = 7.5 * snr_factor * exp(-1.35 * distress);
    double e_dam_rel  = 6.2 * snr_factor * fmax(0.0, distress - 0.15);
    double tremor_adv = fmax(0.0, 0.45 * ((metrics->glottal_tkeo_kurtosis - 2.5) / 4.0) +
                                  0.55 * ((metrics->shimmer_apq5_pct - 3.2) / 2.0));
    double e_dam_adv  = 4.8 * snr_factor * tremor_adv;
    double e_dam_comp = 1.6 * snr_factor * fmax(0.0, fmin(z_dam_anx_aligned, tremor_adv));
    double w_dam_ign  = 2.2 / (snr_factor * fmax(0.35, out_result->shafer_reliability_beta));

    normalize_bpa_from_evidence(e_dam_rem, e_dam_rel, e_dam_adv, e_dam_comp, w_dam_ign, out_result->bpa_dam_disc);

    static const uint32_t focal_masks[5] = {
        DST_MASK_REMISSION,
        DST_MASK_RELAPSE,
        DST_MASK_ADVERSE,
        DST_MASK_DETERIORATION,
        DST_MASK_THETA
    };

    double raw_inter[8];
    memset(raw_inter, 0, sizeof(raw_inter));
    double conflict_k = 0.0;

    for (int i = 0; i < 5; ++i) {
        uint32_t ma = focal_masks[i];
        double mass_a = out_result->bpa_ehr[ma];
        for (int j = 0; j < 5; ++j) {
            uint32_t mb = focal_masks[j];
            double mass_b = out_result->bpa_dam_disc[mb];
            double prod = mass_a * mass_b;
            uint32_t inter = ma & mb;
            if (inter == 0u) {
                conflict_k += prod;
            } else if (inter == DST_MASK_REMISSION ||
                       inter == DST_MASK_RELAPSE ||
                       inter == DST_MASK_ADVERSE ||
                       inter == DST_MASK_DETERIORATION ||
                       inter == DST_MASK_THETA) {
                raw_inter[inter] += prod;
            } else {
                raw_inter[DST_MASK_THETA] += prod;
            }
        }
    }

    conflict_k = clamp_f64(conflict_k, 0.0, 0.999999);
    out_result->dempster_conflict_k = conflict_k;

    double gamma_yager = clamp_f64((conflict_k - 0.25) / 0.55, 0.0, 1.0);
    double denom = fmax(1.0 - conflict_k, COMPASS_EPS);
    double total_fused = 0.0;

    for (int i = 0; i < 5; ++i) {
        uint32_t m = focal_masks[i];
        double dempster_v = raw_inter[m] / denom;
        double yager_v;
        if (m == DST_MASK_THETA) {
            yager_v = raw_inter[m] + conflict_k;
        } else {
            double murphy_avg = 0.32 * out_result->bpa_ehr[m] + 0.68 * out_result->bpa_dam_disc[m];
            yager_v = 0.50 * raw_inter[m] + 0.50 * murphy_avg;
        }
        out_result->bpa_fused[m] = (1.0 - gamma_yager) * dempster_v + gamma_yager * yager_v;
        total_fused += out_result->bpa_fused[m];
    }

    if (total_fused > COMPASS_EPS) {
        for (int i = 0; i < 5; ++i) {
            uint32_t m = focal_masks[i];
            out_result->bpa_fused[m] /= total_fused;
        }
    }

    static const uint32_t singletons[3] = {
        DST_MASK_REMISSION,
        DST_MASK_RELAPSE,
        DST_MASK_ADVERSE
    };
    double bet_sum = 0.0;

    for (int s = 0; s < 3; ++s) {
        uint32_t sm = singletons[s];
        double bel = 0.0;
        double pl  = 0.0;
        double bet = 0.0;

        for (int i = 0; i < 5; ++i) {
            uint32_t bm = focal_masks[i];
            double mass = out_result->bpa_fused[bm];
            if ((bm & sm) == bm) {
                bel += mass;
            }
            if ((bm & sm) != 0u) {
                pl += mass;
                int32_t card = popcount_u32(bm);
                bet += mass / (double)(card > 0 ? card : 1);
            }
        }
        out_result->belief[s]         = bel;
        out_result->plausibility[s]   = pl;
        out_result->pignistic_prob[s] = bet;
        bet_sum += bet;
    }

    int32_t dom_idx = 0;
    if (bet_sum > COMPASS_EPS) {
        for (int s = 0; s < 3; ++s) {
            out_result->pignistic_prob[s] /= bet_sum;
            if (out_result->pignistic_prob[s] > out_result->pignistic_prob[dom_idx]) {
                dom_idx = s;
            }
        }
    }
    out_result->dominant_singleton_idx = dom_idx;
    out_result->epistemic_uncertainty  = out_result->bpa_fused[DST_MASK_THETA];

    double dam_path = out_result->bpa_dam_disc[DST_MASK_RELAPSE] +
                      out_result->bpa_dam_disc[DST_MASK_ADVERSE] +
                      out_result->bpa_dam_disc[DST_MASK_DETERIORATION];
    double ehr_path = out_result->bpa_ehr[DST_MASK_RELAPSE] +
                      out_result->bpa_ehr[DST_MASK_ADVERSE] +
                      out_result->bpa_ehr[DST_MASK_DETERIORATION];

    double dissim = clamp_f64(
        out_result->bpa_ehr[DST_MASK_REMISSION] * dam_path * (1.0 + 0.65 * conflict_k),
        0.0, 1.0
    );
    double aggrav = clamp_f64(
        ehr_path * out_result->bpa_dam_disc[DST_MASK_REMISSION] * (1.0 + 0.50 * conflict_k),
        0.0, 1.0
    );

    out_result->dissimulation_index = dissim;
    out_result->aggravation_index   = aggrav;

    if (dissim >= 0.35) {
        out_result->phenotype_flag_code = PHENOTYPE_MASKED_DISSIMULATION;
        out_result->recommended_tier    = 3;
    } else if (conflict_k >= 0.42 && aggrav >= 0.32) {
        out_result->phenotype_flag_code = PHENOTYPE_SOMATOFORM_AGGRAVATION;
        out_result->recommended_tier    = 2;
    } else if (out_result->pignistic_prob[2] >= 0.38) {
        out_result->phenotype_flag_code = PHENOTYPE_PK_ADVERSE_RISK;
        out_result->recommended_tier    = 3;
    } else if (out_result->pignistic_prob[1] >= 0.45) {
        out_result->phenotype_flag_code = PHENOTYPE_ACTIVE_RELAPSE;
        out_result->recommended_tier    = 2;
    } else {
        out_result->phenotype_flag_code = PHENOTYPE_CONCORDANT_STABLE;
        out_result->recommended_tier    = 1;
    }

    return 0;
}