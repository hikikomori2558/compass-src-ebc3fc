/*
 * c_core/micronets_int8.c
 * =======================
 * Реализация детерминированного C11 W8A8 инференса микросетей
 * и алгоритма конформных предсказательных множеств Mondrian RAPS.
 */

#include "micronets_int8.h"
#include <math.h>
#include <string.h>

#define MNET_EPS 1e-6f

static inline float gelu_approx_f32(float x) {
    float x3 = x * x * x;
    return 0.5f * x * (1.0f + tanhf(0.7978845608f * (x + 0.044715f * x3)));
}

static void layer_norm_inplace_f32(float *vec, int32_t dim) {
    float sum = 0.0f;
    for (int32_t i = 0; i < dim; ++i) {
        sum += vec[i];
    }
    float mean = sum / (float)dim;

    float var_sum = 0.0f;
    for (int32_t i = 0; i < dim; ++i) {
        float d = vec[i] - mean;
        var_sum += d * d;
    }
    float inv_std = 1.0f / sqrtf((var_sum / (float)dim) + MNET_EPS);

    for (int32_t i = 0; i < dim; ++i) {
        vec[i] = (vec[i] - mean) * inv_std;
    }
}

void compass_w8a8_gemv(
    const int8_t *w_int8,
    const float *w_scale_per_channel,
    const float *bias,
    const float *x_fp32,
    int32_t out_dim,
    int32_t in_dim,
    float *y_out
) {
    int8_t x_q[MNET_MAX_HIDDEN];
    float max_abs_x = 0.0f;
    for (int32_t j = 0; j < in_dim; ++j) {
        float ax = fabsf(x_fp32[j]);
        if (ax > max_abs_x) max_abs_x = ax;
    }

    if (max_abs_x <= 1e-8f) {
        for (int32_t i = 0; i < out_dim; ++i) {
            y_out[i] = bias ? bias[i] : 0.0f;
        }
        return;
    }

    float scale_x = max_abs_x / 127.0f;
    float inv_scale_x = 127.0f / max_abs_x;

    for (int32_t j = 0; j < in_dim; ++j) {
        float scaled = nearbyintf(x_fp32[j] * inv_scale_x);
        if (scaled < -127.0f) scaled = -127.0f;
        if (scaled >  127.0f) scaled =  127.0f;
        x_q[j] = (int8_t)scaled;
    }

    for (int32_t i = 0; i < out_dim; ++i) {
        const int8_t *w_row = w_int8 + (size_t)i * (size_t)in_dim;
        int32_t acc_i32 = 0;
        for (int32_t j = 0; j < in_dim; ++j) {
            acc_i32 += (int32_t)w_row[j] * (int32_t)x_q[j];
        }
        float dequant = (float)acc_i32 * (w_scale_per_channel[i] * scale_x);
        y_out[i] = dequant + (bias ? bias[i] : 0.0f);
    }
}

void compass_raps_evaluate_logits3(
    const float logits[MNET_NUM_CLASSES],
    float temperature,
    float lambda_reg,
    int32_t k_reg,
    const float class_quantiles[MNET_NUM_CLASSES],
    uint32_t critical_class_bitmask,
    CompassMicroNetConformalOut *out_res
) {
    memset(out_res, 0, sizeof(CompassMicroNetConformalOut));
    float temp = (temperature > 1e-3f) ? temperature : 1.0f;

    float scaled[3];
    float max_l = logits[0] / temp;
    for (int i = 0; i < 3; ++i) {
        scaled[i] = logits[i] / temp;
        if (scaled[i] > max_l) max_l = scaled[i];
    }

    float exp_sum = 0.0f;
    for (int i = 0; i < 3; ++i) {
        out_res->probabilities[i] = expf(scaled[i] - max_l);
        exp_sum += out_res->probabilities[i];
    }

    float entropy = 0.0f;
    for (int i = 0; i < 3; ++i) {
        out_res->probabilities[i] /= (exp_sum + 1e-12f);
        float p = out_res->probabilities[i];
        if (p > 1e-12f) {
            entropy -= p * logf(p);
        }
    }
    out_res->shannon_entropy_nats = entropy;

    int idx[3] = {0, 1, 2};
    if (out_res->probabilities[idx[0]] < out_res->probabilities[idx[1]]) {
        int t = idx[0]; idx[0] = idx[1]; idx[1] = t;
    }
    if (out_res->probabilities[idx[1]] < out_res->probabilities[idx[2]]) {
        int t = idx[1]; idx[1] = idx[2]; idx[2] = t;
    }
    if (out_res->probabilities[idx[0]] < out_res->probabilities[idx[1]]) {
        int t = idx[0]; idx[0] = idx[1]; idx[1] = t;
    }

    out_res->point_prediction_idx = idx[0];
    out_res->point_confidence     = out_res->probabilities[idx[0]];

    float cum_prob = 0.0f;
    for (int r = 0; r < 3; ++r) {
        int cls = idx[r];
        int rank_1based = r + 1;
        cum_prob += out_res->probabilities[cls];
        float reg_pen = (rank_1based > k_reg)
            ? (lambda_reg * (float)(rank_1based - k_reg))
            : 0.0f;
        out_res->raps_scores[cls] = cum_prob + reg_pen;
    }

    uint32_t set_mask = 0u;
    int32_t set_size  = 0;
    for (int cls = 0; cls < 3; ++cls) {
        if (out_res->raps_scores[cls] <= class_quantiles[cls]) {
            set_mask |= (1u << (uint32_t)cls);
            set_size++;
        }
    }

    int32_t is_null_set = (set_size == 0) ? 1 : 0;
    if (is_null_set) {
        set_mask = (1u << (uint32_t)out_res->point_prediction_idx) | critical_class_bitmask;
        set_size = 0;
        for (int cls = 0; cls < 3; ++cls) {
            if (set_mask & (1u << (uint32_t)cls)) set_size++;
        }
    }

    out_res->prediction_set_bitmask = set_mask;
    out_res->prediction_set_size    = set_size;
    out_res->is_ambiguous_or_ood    = (is_null_set || set_size > 1) ? 1 : 0;

    int32_t has_critical = ((set_mask & critical_class_bitmask) != 0u) ? 1 : 0;
    out_res->mandatory_escalation = (out_res->is_ambiguous_or_ood || has_critical) ? 1 : 0;
}

int32_t compass_micronet_forward_raps(
    const CompassMicroNetDescriptor *net,
    const float *features_24d,
    CompassMicroNetConformalOut *out_res
) {
    if (!net || !features_24d || !out_res) {
        return -1;
    }
    int32_t h_dim = net->hidden_dim;
    if (h_dim <= 0 || h_dim > MNET_MAX_HIDDEN || net->num_blocks < 0 || net->num_blocks > MNET_MAX_BLOCKS) {
        return -2;
    }

    float norm_x[MNET_INPUT_DIM];
    for (int i = 0; i < MNET_INPUT_DIM; ++i) {
        float sd = (net->input_std[i] > 1e-5f) ? net->input_std[i] : 1.0f;
        norm_x[i] = (features_24d[i] - net->input_mean[i]) / sd;
    }

    float h[MNET_MAX_HIDDEN];
    float h_norm[MNET_MAX_HIDDEN];
    float h_mid[MNET_MAX_HIDDEN];
    float h_fc2[MNET_MAX_HIDDEN];

    compass_w8a8_gemv(net->stem_w_int8, net->stem_scale, net->stem_bias, norm_x, h_dim, MNET_INPUT_DIM, h);
    for (int i = 0; i < h_dim; ++i) {
        h[i] = gelu_approx_f32(h[i]);
    }

    for (int b = 0; b < net->num_blocks; ++b) {
        memcpy(h_norm, h, sizeof(float) * (size_t)h_dim);
        layer_norm_inplace_f32(h_norm, h_dim);

        compass_w8a8_gemv(
            net->block_fc1_w_int8[b],
            net->block_fc1_scale[b],
            net->block_fc1_bias[b],
            h_norm,
            h_dim,
            h_dim,
            h_mid
        );
        for (int i = 0; i < h_dim; ++i) {
            h_mid[i] = gelu_approx_f32(h_mid[i]);
        }

        compass_w8a8_gemv(
            net->block_fc2_w_int8[b],
            net->block_fc2_scale[b],
            net->block_fc2_bias[b],
            h_mid,
            h_dim,
            h_dim,
            h_fc2
        );
        for (int i = 0; i < h_dim; ++i) {
            h[i] += 0.5f * h_fc2[i];
        }
    }

    layer_norm_inplace_f32(h, h_dim);
    float logits[MNET_NUM_CLASSES];
    compass_w8a8_gemv(net->head_w_int8, net->head_scale, net->head_bias, h, MNET_NUM_CLASSES, h_dim, logits);

    compass_raps_evaluate_logits3(
        logits,
        net->temperature,
        net->lambda_reg,
        net->k_reg,
        net->class_quantiles,
        net->critical_class_bitmask,
        out_res
    );

    return 0;
}