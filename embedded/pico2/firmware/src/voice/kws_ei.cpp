/* Edge Impulse keyword spotter (CONFIG_EAA_KWS_EDGE_IMPULSE=y).
 * Put the Studio export "C++ library" in firmware/ei-model/ (edge-impulse-sdk/,
 * model-parameters/, tflite-model/). Train with 3+ classes, e.g. "wake", "stop",
 * "noise"/"unknown", 1000 ms window, 16 kHz, MFCC. Continuous mode with 4 slices
 * per window gives one decision every 250 ms (CH-102). */
#include <string.h>
#include <zephyr/kernel.h>
#include "edge-impulse-sdk/classifier/ei_run_classifier.h"
#include "kws.h"

#if EI_CLASSIFIER_FREQUENCY != 16000
#error "Model must be trained at 16 kHz"
#endif

static const int16_t *cur;

static int get_data(size_t offset, size_t length, float *out)
{
	numpy::int16_to_float(cur + offset, out, length);
	return 0;
}

extern "C" int kws_init(void)
{
	run_classifier_init();
	return 0;
}

extern "C" size_t kws_slice_samples(void)
{
	return EI_CLASSIFIER_SLICE_SIZE;
}

extern "C" int kws_run(const int16_t *slice, struct kws_result *r)
{
	signal_t sig;
	ei_impulse_result_t res = {};
	uint32_t t0 = k_cycle_get_32();

	cur = slice;
	sig.total_length = EI_CLASSIFIER_SLICE_SIZE;
	sig.get_data = &get_data;
	if (run_classifier_continuous(&sig, &res, false) != EI_IMPULSE_OK) {
		return -EIO;
	}
	r->time_us = k_cyc_to_us_floor32(k_cycle_get_32() - t0);
	r->label = KWS_NONE;
	r->conf_pct = 0;
	float best = 0.0f;

	for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
		if (res.classification[i].value > best) {
			best = res.classification[i].value;
			const char *l = res.classification[i].label;

			r->label = !strcmp(l, CONFIG_EAA_KWS_LABEL_WAKE)   ? KWS_WAKE
				   : !strcmp(l, CONFIG_EAA_KWS_LABEL_STOP) ? KWS_STOP
									   : KWS_NONE;
		}
	}
	r->conf_pct = (uint8_t)(best * 100.0f + 0.5f);
	return 0;
}

extern "C" const char *kws_backend(void)
{
	return "edge-impulse";
}
