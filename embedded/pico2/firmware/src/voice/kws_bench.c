/* "kws ..." shell commands: benchmark the wake-word pipeline with no microphone.
 *
 *   kws bench   per-clip MFCC + inference timing for both models, int8 output
 *                   compared with the host TFLite interpreter (port parity), label check
 *   kws stream  the bench clips back to back through the real streaming pipeline
 *                   (VAD gate, stride, smoothing, refractory), as if they came from the mic
 *   kws stats   live pipeline counters (frames, inferences, VAD-gated runs, load)
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/sys_io.h>
#include <zephyr/linker/section_tags.h>
#include "kws_engine.h"
#ifdef KWS_FE_CMSIS_DSP
#include <arm_math.h>
#endif

static uint32_t us(uint32_t cyc)
{
	uint32_t hz = kws_cpu_hz();

	return hz ? (uint32_t)((uint64_t)cyc * 1000000u / hz) : 0;
}

static const char *const cls[] = { "silence", "unknown", "WAKE" };
static int16_t clip_ram[KWS_WINDOW];

static void bench_model(const struct shell *sh, const struct kws_model_info *m, bool is_s,
			bool in_ram)
{
	static float mf[KWS_NMFCC];
	static int8_t q[KWS_INPUT_BYTES];
	uint32_t fe_min = UINT32_MAX, fe_max = 0, fe_sum = 0, nn_max = 0, nn_sum = 0, nn_min = UINT32_MAX;
	unsigned int ok = 0, exact = 0, worst = 0;
	int rc = kws_use_model_ex(m, in_ram);

	if (rc) {
		shell_error(sh, "%s: engine init failed (%d), arena too small?", m->name, rc);
		return;
	}
	shell_print(sh, "--- %s, weights in %s: %u B flatbuffer, %u params, %u MACs, arena %u B ---",
		    m->name, in_ram ? "SRAM" : "flash (XIP)", m->len, m->params, m->macs,
		    (unsigned)kws_engine_arena_used());
	for (unsigned int c = 0; c < kws_bench_clip_count; c++) {
		const struct kws_bench_clip *k = &kws_bench_clips[c];
		const int8_t *exp = is_s ? k->expected_s : k->expected_xs;
		int8_t out[3];

		/* No irq_lock / sched_lock here: the 200 Hz control loop, the safety
		 * thread and the watchdog must keep running during a benchmark (the
		 * belt may be live). min = time without preemption. */
		/* live audio sits in SRAM: copy the clip out of flash first */
		memcpy(clip_ram, k->pcm, sizeof(clip_ram));
		for (int f = 0; f < KWS_NFRAMES; f++) {
			uint32_t t0 = kws_cycles();

			(void)kws_fe_frame(&clip_ram[f * KWS_HOP], mf);
			kws_quantize(mf, &q[f * KWS_NMFCC], m);
			uint32_t dt = kws_cycles() - t0;

			fe_sum += dt;
			fe_max = MAX(fe_max, dt);
			fe_min = MIN(fe_min, dt);
		}
		uint32_t t0 = kws_cycles();

		(void)kws_engine_infer(q, out);
		uint32_t dt = kws_cycles() - t0;

		nn_sum += dt;
		nn_max = MAX(nn_max, dt);
		nn_min = MIN(nn_min, dt);
		int best = 0;

		for (int i = 1; i < 3; i++) {
			if (out[i] > out[best]) {
				best = i;
			}
		}
		unsigned int d = 0;

		for (int i = 0; i < 3; i++) {
			d = MAX(d, (unsigned int)abs(out[i] - exp[i]));
		}
		worst = MAX(worst, d);
		exact += d == 0;
		ok += best == k->label;
		int pw = (int)lrintf((out[2] - m->out_zp) * m->out_scale * 100.0f);

		if (in_ram) /* per-clip lines once per model */
			shell_print(sh, "  %-36s truth %-7s -> %-7s  p(wake) %3d%%  |int8 - host| %u",
			    k->name, cls[k->label], cls[best], pw, d);
	}
	unsigned int n = kws_bench_clip_count;

	shell_print(sh, "  correct %u/%u, bit-exact vs host %u/%u (max diff %u LSB)", ok, n,
		    exact, n, worst);
	shell_print(sh, "  MFCC per 20 ms frame: min %u us, avg %u us, max %u us", us(fe_min),
		    us(fe_sum / (n * KWS_NFRAMES)), us(fe_max));
	shell_print(sh, "  inference:  min %u us, avg %u us, max %u us  (%u cycles avg)",
		    us(nn_min), us(nn_sum / n), us(nn_max), nn_sum / n);
	uint32_t hop_us = 20000u * CONFIG_EAA_KWS_STRIDE_HOPS;
	uint32_t busy = us(fe_sum / (n * KWS_NFRAMES)) * CONFIG_EAA_KWS_STRIDE_HOPS + us(nn_sum / n);

	shell_print(sh, "  CPU load while speech (stride %u hops = %u ms): %u.%u %%   quiet: %u.%u %%",
		    CONFIG_EAA_KWS_STRIDE_HOPS, hop_us / 1000, busy * 100 / hop_us,
		    (busy * 1000 / hop_us) % 10, us(fe_sum / (n * KWS_NFRAMES)) * 100 / 20000,
		    (us(fe_sum / (n * KWS_NFRAMES)) * 1000 / 20000) % 10);
}

static int cmd_bench(const struct shell *sh, size_t argc, char **argv)
{
	shell_print(sh, "CPU %u Hz (DWT-calibrated), FFT %s, %u clips", kws_cpu_hz(),
		    IS_ENABLED(CONFIG_EAA_KWS_CMSIS_DSP) ? "CMSIS-DSP rfft_fast_f32" : "portable C",
		    kws_bench_clip_count);
	kws_pause(1);
	bool verbose = argc > 1 && argv[1][0] == 'v';

	ARG_UNUSED(verbose);
	bench_model(sh, &kws_model_s_info, true, false);
	bench_model(sh, &kws_model_s_info, true, true);
	bench_model(sh, &kws_model_xs_info, false, false);
	bench_model(sh, &kws_model_xs_info, false, true);
	(void)kws_use_model(kws_selected_model());
	kws_pause(0);
	return 0;
}

static int cmd_stream(const struct shell *sh, size_t argc, char **argv)
{
	static int16_t gap[KWS_HOP];
	struct kws_stats s;
	uint32_t t = 0;
	unsigned int wakes = 0, hits = 0, fa = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	kws_pause(1);
	(void)kws_use_model(argc > 1 && argv[1][0] == 'x' ? &kws_model_xs_info : kws_selected_model());
	kws_get_stats(&s, 1);
	/* 0.5 s of near-silence between clips so the VAD and the window reset as in real use */
	for (int i = 0; i < KWS_HOP; i++) {
		gap[i] = (int16_t)((i * 7919) % 9) - 4;
	}
	for (unsigned int c = 0; c < kws_bench_clip_count; c++) {
		const struct kws_bench_clip *k = &kws_bench_clips[c];
		bool fired = false;

		wakes += k->label == KWS_CLASS_WAKE;
		for (int h = 0; h < 25 + KWS_WINDOW / KWS_HOP + 25; h++, t += 20) {
			const int16_t *src = gap;

			if (h >= 25 && h < 25 + KWS_WINDOW / KWS_HOP) {
				src = &k->pcm[(h - 25) * KWS_HOP];
			}
			uint8_t score;

			if (kws_pipeline_hop(src, &score)) {
				shell_print(sh, "  t=%5u ms WAKE %u%% during %s", t, score, k->name);
				fired = true;
			}
		}
		if (fired) {
			hits += k->label == KWS_CLASS_WAKE;
			fa += k->label != KWS_CLASS_WAKE;
		}
	}
	kws_get_stats(&s, 0);
	shell_print(sh, "stream %s: %u/%u wake clips detected, %u false wakes; %u frames, "
		    "%u inferences, %u skipped by VAD (%u%%)",
		    kws_engine_model()->name, hits, wakes, fa, s.frames, s.inferences, s.gated,
		    s.gated * 100 / MAX(1u, s.inferences + s.gated));
	(void)kws_use_model(kws_selected_model());
	kws_pause(0);
	return 0;
}

static int cmd_stats(const struct shell *sh, size_t argc, char **argv)
{
	struct kws_stats s;
	bool reset = argc > 1 && argv[1][0] == 'r';

	kws_get_stats(&s, reset);
	shell_print(sh, "model %s, %u frames, %u inferences, %u VAD-skipped, %u detections",
		    kws_engine_model() ? kws_engine_model()->name : "-", s.frames, s.inferences,
		    s.gated, s.detections);
	shell_print(sh, "level %d dBFS, noise floor %d dBFS, last score %u%%", (int)s.last_db,
		    (int)s.noise_floor_db, s.last_score_pct);
	if (s.frames) {
		shell_print(sh, "MFCC avg %u us max %u us; inference avg %u us max %u us",
			    us((uint32_t)(s.fe_cycles_sum / s.frames)), us(s.fe_cycles_max),
			    s.inferences ? us((uint32_t)(s.nn_cycles_sum / s.inferences)) : 0,
			    us(s.nn_cycles_max));
	}
	return 0;
}

/* Where the MFCC time goes: each stage 200 times on a real clip. */
static int cmd_profile(const struct shell *sh, size_t argc, char **argv)
{
	static float buf[KWS_FFT], spec[KWS_FFT], pw[KWS_NBINS], mf[KWS_NMFCC];
	const int16_t *pcm = clip_ram;
	const int n = 200;
	uint32_t t0, c;
	volatile float sink = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	memcpy(clip_ram, kws_bench_clips[0].pcm, sizeof(clip_ram));
	t0 = kws_cycles();
	for (int i = 0; i < n; i++) {
		(void)kws_fe_frame(&pcm[(i % 40) * KWS_HOP], mf);
	}
	c = (kws_cycles() - t0) / n;
	shell_print(sh, "whole frame      %6u cycles  %4u us", c, us(c));
	for (int k = 0; k < KWS_FFT; k++) {
		buf[k] = pcm[k] * (1.0f / 32768.0f);
	}
#ifdef KWS_FE_CMSIS_DSP
	arm_rfft_fast_instance_f32 r;

	arm_rfft_fast_init_f32(&r, KWS_FFT);
	t0 = kws_cycles();
	for (int i = 0; i < n; i++) {
		arm_rfft_fast_f32(&r, buf, spec, 0);
	}
	c = (kws_cycles() - t0) / n;
	shell_print(sh, "rfft 512 (tables in flash) %6u cycles  %4u us", c, us(c));
	t0 = kws_cycles();
	for (int i = 0; i < n; i++) {
		arm_cmplx_mag_squared_f32(&spec[2], &pw[1], KWS_FFT / 2 - 1);
	}
	c = (kws_cycles() - t0) / n;
	shell_print(sh, "mag^2 255 bins   %6u cycles  %4u us", c, us(c));
#endif
	t0 = kws_cycles();
	for (int i = 0; i < n; i++) {
		for (int m = 0; m < KWS_NMEL; m++) {
			sink += logf(pw[m + 1] + 1e-6f + (float)i);
		}
	}
	c = (kws_cycles() - t0) / n;
	shell_print(sh, "40 x logf        %6u cycles  %4u us", c, us(c));
	t0 = kws_cycles();
	for (int i = 0; i < n; i++) {
		for (int k = 0; k < KWS_FRAME_LEN; k++) {
			buf[k] = pcm[k] * (1.0f / 32768.0f) * (float)(i + 1);
		}
	}
	c = (kws_cycles() - t0) / n;
	shell_print(sh, "480 int16->float %6u cycles  %4u us", c, us(c));
	ARG_UNUSED(sink);
	return 0;
}

/* Same float kernel placed in SRAM and in XIP flash: tells whether code
 * fetch from flash is what slows the MCU down. */
#define MB_N 256
static float mb_a[MB_N], mb_b[MB_N];

static __noinline float mb_flash(int reps)
{
	float acc = 0;

	for (int r = 0; r < reps; r++) {
		for (int i = 0; i < MB_N; i++) {
			acc += mb_a[i] * mb_b[i];
		}
	}
	return acc;
}

static __noinline __ramfunc float mb_ram(int reps)
{
	float acc = 0;

	for (int r = 0; r < reps; r++) {
		for (int i = 0; i < MB_N; i++) {
			acc += mb_a[i] * mb_b[i];
		}
	}
	return acc;
}

static int cmd_cpu(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t t0, c1, c2;
	volatile float sink;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	for (int i = 0; i < MB_N; i++) {
		mb_a[i] = i * 0.5f;
		mb_b[i] = 1.0f / (i + 1);
	}
	t0 = kws_cycles();
	sink = mb_flash(100);
	c1 = kws_cycles() - t0;
	t0 = kws_cycles();
	sink = mb_ram(100);
	c2 = kws_cycles() - t0;
	/* 2560 MACs (~0.15 ms) with interrupts off vs on: shows the interrupt load */
	unsigned int key = irq_lock();

	t0 = kws_cycles();
	sink = mb_ram(10);
	uint32_t c3 = kws_cycles() - t0;

	irq_unlock(key);
	t0 = kws_cycles();
	sink = mb_ram(10);
	uint32_t c4 = kws_cycles() - t0;

	shell_print(sh, "2560 MACs from SRAM: irq off %u cycles, irq on %u cycles", c3, c4);
	ARG_UNUSED(sink);
	shell_print(sh, "25600 float MACs: code in flash %u cycles (%u.%02u /MAC), code in SRAM %u cycles"
		    " (%u.%02u /MAC)", c1, c1 / 25600, (c1 % 25600) * 100 / 25600, c2, c2 / 25600,
		    (c2 % 25600) * 100 / 25600);
#if defined(CONFIG_SOC_SERIES_RP2350)
	/* RP2350 QMI (XIP flash interface), datasheet ch. 12.14 */
	uint32_t timing = sys_read32(0x400D000C), rfmt = sys_read32(0x400D0010),
		 rcmd = sys_read32(0x400D0014);
	static const char *const w[] = { "S", "D", "Q", "?" };

	shell_print(sh, "QMI M0: clkdiv %u (flash SCK %u MHz), read cmd 0x%02x, addr %s data %s, "
		    "dummy %u bits", timing & 0xff, kws_cpu_hz() / 1000000 / MAX(1u, timing & 0xff),
		    rcmd & 0xff, w[(rfmt >> 2) & 3], w[(rfmt >> 8) & 3], (rfmt >> 16) & 0x7);
#endif
	return 0;
}

static int cmd_vad(const struct shell *sh, size_t argc, char **argv)
{
	bool on = !(argc > 1 && argv[1][1] == 'f');

	kws_set_vad(on);
	shell_print(sh, "VAD gate %s%s", on ? "on" : "OFF",
		    on ? "" : " - model runs every stride (worst-case CPU load test)");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(kws_cmds,
	SHELL_CMD_ARG(vad, NULL, "vad on|off  (off = worst-case load test)", cmd_vad, 2, 0),
	SHELL_CMD(cpu, NULL, "Flash vs SRAM code speed, XIP configuration", cmd_cpu),
	SHELL_CMD(profile, NULL, "Cycle profile of the MFCC stages", cmd_profile),
	SHELL_CMD(bench, NULL, "Benchmark both models on the clips stored in flash", cmd_bench),
	SHELL_CMD_ARG(stream, NULL, "stream [xs]  clips through the streaming pipeline",
		      cmd_stream, 1, 1),
	SHELL_CMD_ARG(stats, NULL, "stats [reset]  live pipeline counters", cmd_stats, 1, 1),
	SHELL_SUBCMD_SET_END);
SHELL_CMD_REGISTER(kws, &kws_cmds, "Wake word 'Hey Edgie' (TFLite Micro)", NULL);
