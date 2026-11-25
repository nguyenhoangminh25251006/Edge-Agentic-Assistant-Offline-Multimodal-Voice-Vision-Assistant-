/* Wake-word pipeline on the MCU (see embedded/kws/README.md for the design).
 *
 *  mic 16 kHz -> [MFCC every 20 ms] -> [energy VAD gate] -> [DS-CNN int8 every
 *  STRIDE hops, only while speech] -> [mean of the last SMOOTH scores] -> wake
 */
#ifndef EAA_KWS_ENGINE_H
#define EAA_KWS_ENGINE_H
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include "kws_frontend.h"
#include "model/kws_models.h"

#ifdef __cplusplus
extern "C" {
#endif

#define KWS_INPUT_BYTES (KWS_NFRAMES * KWS_NMFCC)

/* TFLite Micro runner (kws_engine.cpp) */
int kws_engine_init(const struct kws_model_info *m);
int kws_engine_infer(const int8_t *features, int8_t out[3]);
size_t kws_engine_arena_used(void);
const struct kws_model_info *kws_engine_model(void);

/* Streaming pipeline (kws_pipeline.c) */
struct kws_stats {
	uint32_t frames;        /* MFCC frames computed */
	uint32_t inferences;    /* model runs */
	uint32_t gated;         /* model runs skipped by the VAD */
	uint32_t detections;
	uint32_t fe_cycles_max, nn_cycles_max;
	uint64_t fe_cycles_sum, nn_cycles_sum;   /* 64-bit: 470 runs x 10 M cycles overflow 32 bits */
	float noise_floor_db;
	float last_db;
	uint8_t last_score_pct;
};
const struct kws_model_info *kws_selected_model(void);
void kws_pipeline_reset(void);
/* Feed one hop (KWS_HOP samples). Returns 1 when the wake word fires and sets
 * *score_pct to the smoothed confidence; 0 otherwise. */
int kws_pipeline_hop(const int16_t *hop, uint8_t *score_pct);
void kws_get_stats(struct kws_stats *out, int reset);
void kws_set_vad(int enabled);
/* Stop the live (microphone) pipeline while the benchmark uses the engine. */
void kws_pause(int on);
int kws_use_model(const struct kws_model_info *m);
int kws_use_model_ex(const struct kws_model_info *m, bool in_ram);
/* DWT cycle counter helpers */
void kws_cycles_init(void);
uint32_t kws_cycles(void);
uint32_t kws_cpu_hz(void);
void kws_quantize(const float *mfcc, int8_t *q, const struct kws_model_info *m);

#ifdef __cplusplus
}
#endif
#endif
