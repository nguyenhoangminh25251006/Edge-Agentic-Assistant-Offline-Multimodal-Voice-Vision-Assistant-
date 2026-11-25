/* Always-on wake-word pipeline ("Hey Edgie") - the cheap part that never sleeps.
 *
 * Per 20 ms hop:   MFCC of the newest 30 ms frame (~0.1 ms), energy VAD.
 * Per STRIDE hops: DS-CNN int8 on the last 1 s (49 x 10 features), but only
 *                  while the VAD saw speech in the last second - in a quiet
 *                  room the model never runs.
 * Decision:        mean of the last SMOOTH wake scores >= threshold, then a
 *                  refractory period. The MCU then raises VOICE_EVENT WAKE and
 *                  the Linux side (Vosk STT + SLM) is woken; nothing on the MPU
 *                  has to listen continuously.
 */
#include <math.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <cmsis_core.h>
#include "kws.h"
#include "kws_engine.h"

#define HANG_FRAMES     KWS_NFRAMES /* keep the model on for 1 s after speech */
#define REFRACTORY      75          /* 1.5 s after a detection */
#define SMOOTH          CONFIG_EAA_KWS_SMOOTH

static int16_t tail[KWS_FRAME_LEN]; /* newest samples, frame being built */
static int8_t feat[KWS_NFRAMES * KWS_NMFCC];
static uint32_t filled;            /* frames in feat (saturates at 49) */
static uint32_t hop_no;
static int hang, refractory;
static uint8_t hist[SMOOTH];
static uint8_t hist_i;
static bool vad_on = true;
static struct kws_stats st;
static const struct kws_model_info *model;
static uint32_t cpu_hz;
static atomic_t paused; /* benchmark owns the pipeline */

void kws_pause(int on)
{
	atomic_set(&paused, on);
}

const struct kws_model_info *kws_selected_model(void)
{
	return IS_ENABLED(CONFIG_EAA_KWS_MODEL_XS) ? &kws_model_xs_info : &kws_model_s_info;
}

void kws_cycles_init(void)
{
	DCB->DEMCR |= DCB_DEMCR_TRCENA_Msk;
	DWT->CYCCNT = 0;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
	if (!cpu_hz) {
		uint32_t t0 = DWT->CYCCNT;

		k_busy_wait(20000);
		uint32_t c = DWT->CYCCNT - t0;

		cpu_hz = (uint32_t)((uint64_t)c * 50u); /* cycles in 20 ms -> Hz */
	}
}

uint32_t kws_cycles(void)
{
	return DWT->CYCCNT;
}

uint32_t kws_cpu_hz(void)
{
	return cpu_hz;
}

void kws_quantize(const float *mfcc, int8_t *q, const struct kws_model_info *m)
{
	const float inv = 1.0f / m->in_scale;

	for (int i = 0; i < KWS_NMFCC; i++) {
		int v = (int)lrintf(mfcc[i] * inv) + m->in_zp;

		q[i] = (int8_t)(v < -128 ? -128 : (v > 127 ? 127 : v));
	}
}

void kws_set_vad(int enabled)
{
	vad_on = enabled != 0;
}

void kws_pipeline_reset(void)
{
	memset(tail, 0, sizeof(tail));
	memset(feat, 0, sizeof(feat));
	memset(hist, 0, sizeof(hist));
	filled = hop_no = 0;
	hang = refractory = 0;
	st.noise_floor_db = -60.0f;
}

void kws_get_stats(struct kws_stats *out, int reset)
{
	*out = st;
	if (reset) {
		float nf = st.noise_floor_db;

		memset(&st, 0, sizeof(st));
		st.noise_floor_db = nf;
	}
}

int kws_pipeline_hop(const int16_t *hop, uint8_t *score_pct)
{
	float mfcc[KWS_NMFCC];
	uint32_t t0 = kws_cycles();

	/* slide the 30 ms frame by one 20 ms hop */
	memmove(tail, &tail[KWS_HOP], sizeof(int16_t) * (KWS_FRAME_LEN - KWS_HOP));
	memcpy(&tail[KWS_FRAME_LEN - KWS_HOP], hop, sizeof(int16_t) * KWS_HOP);
	float db = kws_fe_frame(tail, mfcc);

	memmove(feat, &feat[KWS_NMFCC], KWS_INPUT_BYTES - KWS_NMFCC);
	kws_quantize(mfcc, &feat[KWS_INPUT_BYTES - KWS_NMFCC], model);
	if (filled < KWS_NFRAMES) {
		filled++;
	}
	uint32_t dt = kws_cycles() - t0;

	st.frames++;
	st.fe_cycles_sum += dt;
	st.fe_cycles_max = MAX(st.fe_cycles_max, dt);
	st.last_db = db;

	/* energy VAD: noise floor follows quiet frames quickly, loud ones slowly */
	float *nf = &st.noise_floor_db;

	*nf += (db < *nf ? 0.3f : 0.004f) * (db - *nf);
	if (*nf < -90.0f) {
		*nf = -90.0f;
	}
	if (db > *nf + CONFIG_EAA_KWS_VAD_DB && db > -70.0f) {
		hang = HANG_FRAMES;
	} else if (hang > 0) {
		hang--;
	}
	if (refractory > 0) {
		refractory--;
	}

	if (++hop_no % CONFIG_EAA_KWS_STRIDE_HOPS != 0 || filled < KWS_NFRAMES) {
		return 0;
	}
	if (vad_on && hang == 0) {
		st.gated++;
		memset(hist, 0, sizeof(hist));
		return 0;
	}

	int8_t out[3];

	t0 = kws_cycles();
	if (kws_engine_infer(feat, out) != 0) {
		return 0;
	}
	dt = kws_cycles() - t0;
	st.inferences++;
	st.nn_cycles_sum += dt;
	st.nn_cycles_max = MAX(st.nn_cycles_max, dt);

	float p = (out[KWS_CLASS_WAKE] - model->out_zp) * model->out_scale;
	unsigned int sum = 0;

	hist[hist_i] = (uint8_t)CLAMP(lrintf(p * 100.0f), 0, 100);
	hist_i = (hist_i + 1) % SMOOTH;
	for (int i = 0; i < SMOOTH; i++) {
		sum += hist[i];
	}
	st.last_score_pct = (uint8_t)(sum / SMOOTH);
	if (st.last_score_pct >= CONFIG_EAA_KWS_THRESHOLD_PCT && refractory == 0) {
		refractory = REFRACTORY;
		memset(hist, 0, sizeof(hist));
		st.detections++;
		*score_pct = st.last_score_pct;
		return 1;
	}
	return 0;
}

/* Weights in SRAM instead of XIP flash: on the RP2350 the model (41 KB for
 * DS-CNN-S) does not fit the 16 KB XIP cache, so every inference re-reads it
 * over QSPI. The STM32U585 runs from internal flash with the ART cache. */
static uint8_t __aligned(16) ram_model[CONFIG_EAA_KWS_MODEL_RAM_KB * 1024 + 16];
static struct kws_model_info ram_info;

int kws_use_model_ex(const struct kws_model_info *m, bool in_ram)
{
	if (in_ram && m->len <= sizeof(ram_model)) {
		if (ram_info.data != ram_model || ram_info.len != m->len ||
		    ram_info.name != m->name) {
			kws_engine_init(NULL); /* engine off while the buffer changes */
			memcpy(ram_model, m->data, m->len);
			ram_info = *m;
			ram_info.data = ram_model;
		}
		m = &ram_info;
	}
	model = m;
	kws_pipeline_reset();
	return kws_engine_init(m);
}

int kws_use_model(const struct kws_model_info *m)
{
	return kws_use_model_ex(m, CONFIG_EAA_KWS_MODEL_RAM_KB > 0);
}

/* ---- kws.h backend used by voice.c ---- */
int kws_init(void)
{
	kws_fe_init();
	kws_cycles_init();
	return kws_use_model(kws_selected_model());
}

size_t kws_slice_samples(void)
{
	return KWS_HOP; /* voice.c feeds one hop at a time */
}

int kws_run(const int16_t *slice, struct kws_result *r)
{
	uint8_t score = 0;

	if (atomic_get(&paused)) {
		r->label = KWS_NONE;
		r->conf_pct = 0;
		r->time_us = 0;
		return 0;
	}
	uint32_t t0 = kws_cycles();
	int hit = kws_pipeline_hop(slice, &score);

	r->label = hit ? KWS_WAKE : KWS_NONE;
	r->conf_pct = hit ? score : st.last_score_pct;
	r->time_us = cpu_hz ? (uint32_t)((uint64_t)(kws_cycles() - t0) * 1000000u / cpu_hz) : 0;
	return 0;
}

const char *kws_backend(void)
{
	return IS_ENABLED(CONFIG_EAA_KWS_MODEL_XS) ? "TFLM DS-CNN-XS 'Hey Edgie'"
						   : "TFLM DS-CNN-S 'Hey Edgie'";
}
