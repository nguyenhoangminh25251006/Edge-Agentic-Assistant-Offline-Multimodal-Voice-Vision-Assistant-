/* Keyword spotter interface (CH-102 / CH-103): 3 classes wake / stop / other,
 * 1 s window at 16 kHz, decision every 250 ms (one "slice"). */
#ifndef EAA_KWS_H
#define EAA_KWS_H
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KWS_NONE 0
#define KWS_WAKE 1
#define KWS_STOP 2

struct kws_result {
	uint8_t label;    /* KWS_* with the highest score */
	uint8_t conf_pct; /* its score, 0..100 */
	uint32_t time_us; /* inference time of this slice */
};

int kws_init(void);
size_t kws_slice_samples(void);
/* Feed exactly kws_slice_samples() samples; returns 0 or a negative error. */
int kws_run(const int16_t *slice, struct kws_result *r);
const char *kws_backend(void);

#ifdef __cplusplus
}
#endif
#endif
