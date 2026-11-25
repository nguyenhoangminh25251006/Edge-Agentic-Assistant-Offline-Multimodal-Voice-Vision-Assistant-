/* Voice front end on the MCU (team decision in CH-102, task CH-103 / CH-243).
 *
 *  mic (16 kHz) -> keyword spotter every 250 ms (low priority, same core as the
 *  motor loop, exactly like the STM32U585 on the UNO Q)
 *    "stop"  -> the MCU stops the belt itself (no wake word, no MPU needed),
 *               then VOICE_EVENT kind STOP
 *    wake    -> VOICE_ACTIVE flag + listening LED + VOICE_EVENT kind WAKE,
 *               then up to 5 s of command audio on the audio channel for STT
 *  wake word "Hey Edgie": kws_pipeline.c (MFCC + VAD + DS-CNN, always on)
 *  push-to-talk button = wake (test aid)
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/logging/log.h>
#include "eaa.h"
#include "audio_in.h"
#include "audio_out.h"
#include "kws.h"

LOG_MODULE_REGISTER(voice, LOG_LEVEL_INF);

#define USER DT_PATH(zephyr_user)
#define CHUNK 256
#define STOP_HOLDOFF_MS 1000
#define MIC_CHECK_MS 1000

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET_OR(USER, listen_led_gpios, {0});
static const struct gpio_dt_spec ptt = GPIO_DT_SPEC_GET_OR(USER, ptt_gpios, {0});

static int16_t slice[4000];
static atomic_t sim_wake, sim_stop;
static bool mic_running;

void voice_simulate_wake(void)
{
	atomic_set(&sim_wake, 1);
}

void voice_simulate_stop(void)
{
	atomic_set(&sim_stop, 1);
}

static void do_stop(uint8_t conf)
{
	safety_voice_stop();
	bridge_emit_voice(B03_VOICE_STOP, conf, CONFIG_EAA_KWS_MODEL_VER);
	LOG_INF("voice STOP (%u%%) -> belt stopped by MCU", conf);
}

static void voice_thread(void *a, void *b, void *c)
{
	size_t slice_len = MIN(kws_slice_samples(), ARRAY_SIZE(slice));
	size_t fill = 0;
	int16_t chunk[CHUNK];
	bool window = false;
	int64_t window_end = 0, stop_ok_at = 0, mic_check_at = 0;
	uint32_t streamed = 0;
	uint16_t wake_no = 0;
	bool ptt_prev = false;
	uint8_t wake_conf = 100; /* push-to-talk / shell = 100 % */
	int16_t lo = INT16_MAX, hi = INT16_MIN;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		size_t n = mic_running ? audio_in_read(chunk, CHUNK, K_MSEC(20)) : 0;
		int64_t now = k_uptime_get();
		bool want_wake = atomic_set(&sim_wake, 0) != 0;
		bool want_stop = atomic_set(&sim_stop, 0) != 0;

		if (!mic_running) {
			k_sleep(K_MSEC(20));
		}

		/* mic health: a dead or unwired mic gives a constant value */
		for (size_t i = 0; i < n; i++) {
			lo = MIN(lo, chunk[i]);
			hi = MAX(hi, chunk[i]);
		}
		if (now >= mic_check_at) {
			bool ok = mic_running && (hi - lo) > 4;

			safety_set_mic_ok(ok || !mic_running);
			mic_check_at = now + MIC_CHECK_MS;
			lo = INT16_MAX;
			hi = INT16_MIN;
		}

		/* push-to-talk edge */
		if (ptt.port != NULL) {
			bool p = gpio_pin_get_dt(&ptt) == 1;

			if (p && !ptt_prev) {
				want_wake = true;
			}
			ptt_prev = p;
		}

		/* command window */
		if (window) {
			if (n > 0) {
				audio_out_samples(chunk, n);
				streamed += n;
			}
			if (now >= window_end) {
				audio_out_end(streamed);
				window = false;
				safety_set_voice_active(false);
				if (led.port) {
					gpio_pin_set_dt(&led, 0);
				}
				LOG_INF("command window closed, %u samples sent", streamed);
			}
		}

		/* keyword spotter, one decision per slice */
		for (size_t i = 0; i < n; i++) {
			slice[fill++] = chunk[i];
			if (fill == slice_len) {
				struct kws_result r;

				fill = 0;
				if (kws_run(slice, &r) == 0 &&
				    r.conf_pct >= CONFIG_EAA_KWS_THRESHOLD_PCT) {
					if (r.label == KWS_STOP) {
						want_stop = true;
					} else if (r.label == KWS_WAKE) {
						want_wake = true;
						wake_conf = r.conf_pct;
					}
				}
			}
		}

		if (want_stop && now >= stop_ok_at) {
			do_stop(want_stop ? 100 : 0);
			stop_ok_at = now + STOP_HOLDOFF_MS;
			if (window) {
				audio_out_end(streamed);
				window = false;
				if (led.port) {
					gpio_pin_set_dt(&led, 0);
				}
			}
		} else if (want_wake && !window) {
			window = true;
			window_end = now + CONFIG_EAA_CMD_WINDOW_MS;
			streamed = 0;
			wake_no++;
			safety_set_voice_active(true);
			if (led.port) {
				gpio_pin_set_dt(&led, 1);
			}
			bridge_emit_voice(B03_VOICE_WAKE, wake_conf, CONFIG_EAA_KWS_MODEL_VER);
			wake_conf = 100;
			audio_out_begin(wake_no, CONFIG_EAA_AUDIO_SAMPLE_RATE);
			LOG_INF("WAKE -> listening for %d ms", CONFIG_EAA_CMD_WINDOW_MS);
		}
	}
}

K_THREAD_STACK_DEFINE(voice_stack, 4096);
static struct k_thread voice_tid;

int voice_init(void)
{
	if (led.port && gpio_is_ready_dt(&led)) {
		gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	}
	if (ptt.port && gpio_is_ready_dt(&ptt)) {
		gpio_pin_configure_dt(&ptt, GPIO_INPUT);
	}
	(void)audio_out_init();
	kws_init();
	mic_running = audio_in_init(CONFIG_EAA_AUDIO_SAMPLE_RATE) == 0;
	LOG_INF("voice: keyword spotter = %s, slice %u samples", kws_backend(),
		(unsigned)kws_slice_samples());
	return mic_running ? 0 : -EIO;
}

void voice_start(void)
{
	k_thread_create(&voice_tid, voice_stack, K_THREAD_STACK_SIZEOF(voice_stack),
			voice_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(8), 0, K_NO_WAIT);
	k_thread_name_set(&voice_tid, "voice");
}
