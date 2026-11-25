/* Placeholder keyword spotter used until the Edge Impulse model is trained.
 * It never reports a keyword; it only measures the signal level so the mic
 * wiring can be checked. The wake path is tested with the push-to-talk button
 * or the shell command "eaa wake"; the stop path with "eaa vstop". */
#include <zephyr/kernel.h>
#include "kws.h"

int kws_init(void)
{
	return 0;
}

size_t kws_slice_samples(void)
{
	return 4000; /* 250 ms at 16 kHz, same stride as the Edge Impulse model */
}

int kws_run(const int16_t *slice, struct kws_result *r)
{
	ARG_UNUSED(slice);
	r->label = KWS_NONE;
	r->conf_pct = 0;
	r->time_us = 0;
	return 0;
}

const char *kws_backend(void)
{
	return "stub (push-to-talk)";
}
