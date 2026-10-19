/*
 * c_core/micronets_int8.h
 * =======================
 * Нативный C11 W8A8 (INT8 Weight x INT8 Activation -> INT32 Accumulator)
 * движок инференса управляющих микросетей и конформной калибровки Mondrian RAPS.
 *
 * Проект: COMPASS-HYBRID v2.6 (Команда Rapira)
 */

#ifndef COMPASS_MICRONETS_INT8_H
#define COMPASS_MICRONETS_INT8_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MNET_INPUT_DIM     24
#define MNET_NUM_CLASSES   3
#define MNET_MAX_HIDDEN    256
#define MNET_MAX_BLOCKS    4

typedef struct {
    float probabilities[MNET_NUM_CLASSES];
    float raps_scores[MNET_NUM_CLASSES];
    float point_confidence;
    float shannon_entropy_nats;
    int32_t point_prediction_idx;
    uint32_t prediction_set_bitmask;
    int32_t prediction_set_size;
    int32_t is_ambiguous_or_ood;
    int32_t mandatory_escalation;
} CompassMicroNetConformalOut;

typedef struct {
    int32_t hidden_dim;
    int32_t num_blocks;
    float input_mean[MNET_INPUT_DIM];
    float input_std[MNET_INPUT_DIM];

    const int8_t *stem_w_int8;
    const float  *stem_scale;
    const float  *stem_bias;

    const int8_t *block_fc1_w_int8[MNET_MAX_BLOCKS];
    const float  *block_fc1_scale[MNET_MAX_BLOCKS];
    const float  *block_fc1_bias[MNET_MAX_BLOCKS];

    const int8_t *block_fc2_w_int8[MNET_MAX_BLOCKS];
    const float  *block_fc2_scale[MNET_MAX_BLOCKS];
    const float  *block_fc2_bias[MNET_MAX_BLOCKS];

    const int8_t *head_w_int8;
    const float  *head_scale;
    const float  *head_bias;

    float temperature;
    float lambda_reg;
    int32_t k_reg;
    float class_quantiles[MNET_NUM_CLASSES];
    uint32_t critical_class_bitmask;
} CompassMicroNetDescriptor;

void compass_w8a8_gemv(
    const int8_t *w_int8,
    const float *w_scale_per_channel,
    const float *bias,
    const float *x_fp32,
    int32_t out_dim,
    int32_t in_dim,
    float *y_out
);

int32_t compass_micronet_forward_raps(
    const CompassMicroNetDescriptor *net,
    const float *features_24d,
    CompassMicroNetConformalOut *out_res
);

void compass_raps_evaluate_logits3(
    const float logits[MNET_NUM_CLASSES],
    float temperature,
    float lambda_reg,
    int32_t k_reg,
    const float class_quantiles[MNET_NUM_CLASSES],
    uint32_t critical_class_bitmask,
    CompassMicroNetConformalOut *out_res
);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_MICRONETS_INT8_H */