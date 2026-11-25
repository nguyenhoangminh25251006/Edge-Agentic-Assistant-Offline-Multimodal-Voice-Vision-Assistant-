/* Wake-word MFCC front end, see kws_frontend.h.
 * FFT: KWS_FE_CMSIS_DSP -> arm_rfft_fast_f32 (CMSIS-DSP, Cortex-M33 FPU/DSP),
 *      otherwise a portable radix-2 real FFT (host build, fallback). */
#include <math.h>
#include <string.h>
#include "kws_frontend.h"

#ifdef KWS_FE_CMSIS_DSP
#include <arm_math.h>
#include <arm_common_tables.h>
static arm_rfft_fast_instance_f32 rfft;
/* CMSIS-DSP keeps its FFT tables in flash. On the RP2350 flash is behind a
 * 16 KB XIP cache on a 50 MHz dual-SPI bus (~150 CPU cycles per cache miss),
 * so the tables are copied to SRAM: 512-point rfft 84.6k -> see report. */
static float tw_cfft[2 * KWS_FFT / 2], tw_rfft[KWS_FFT];
static uint16_t br_cfft[ARMBITREVINDEXTABLE_256_TABLE_LENGTH];
#else
#define NC (KWS_FFT / 2) /* complex FFT size for the packed real FFT */
static float tw_c[NC / 2], tw_s[NC / 2]; /* complex FFT twiddles */
static float sp_c[NC], sp_s[NC];         /* real-split twiddles */
static uint16_t bitrev[NC];
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static float win[KWS_FRAME_LEN];
/* Each FFT bin feeds at most two neighbouring mel bands: band mel_lo[k] with
 * weight mel_w[k] and band mel_lo[k] + 1 with weight 1 - mel_w[k]... expressed
 * as separate lower/upper weights to keep the triangles exact at the edges. */
static int8_t mel_lo[KWS_NBINS];
static float mel_wlo[KWS_NBINS], mel_whi[KWS_NBINS];
static float dct[KWS_NMFCC][KWS_NMEL];
static float buf[KWS_FFT];
#ifdef KWS_FE_CMSIS_DSP
static float spec[KWS_FFT];
#endif
static int ready;

static double hz_to_mel(double f)
{
	return 2595.0 * log10(1.0 + f / 700.0);
}

static double mel_to_hz(double m)
{
	return 700.0 * (pow(10.0, m / 2595.0) - 1.0);
}

void kws_fe_init(void)
{
	int k, m;

	for (k = 0; k < KWS_FRAME_LEN; k++) { /* periodic Hann */
		win[k] = (float)(0.5 - 0.5 * cos(2.0 * M_PI * k / KWS_FRAME_LEN));
	}

	/* mel triangles on FFT bin frequencies */
	double edges[KWS_NMEL + 2];
	double lo = hz_to_mel(20.0), hi = hz_to_mel(8000.0);

	for (m = 0; m < KWS_NMEL + 2; m++) {
		edges[m] = mel_to_hz(lo + (hi - lo) * m / (KWS_NMEL + 1));
	}
	for (k = 0; k < KWS_NBINS; k++) {
		double f = (double)k * KWS_SR / KWS_FFT;

		mel_lo[k] = -1;
		mel_wlo[k] = 0.0f;
		mel_whi[k] = 0.0f;
		for (m = 0; m <= KWS_NMEL; m++) { /* segment [edges[m], edges[m+1]] */
			if (f >= edges[m] && f < edges[m + 1]) {
				double t = (f - edges[m]) / (edges[m + 1] - edges[m]);

				/* rising edge of band m, falling edge of band m-1 */
				mel_lo[k] = (int8_t)(m - 1);
				mel_wlo[k] = (m >= 1) ? (float)(1.0 - t) : 0.0f; /* band m-1 */
				mel_whi[k] = (m < KWS_NMEL) ? (float)t : 0.0f;    /* band m */
				break;
			}
		}
	}

	for (m = 0; m < KWS_NMFCC; m++) { /* orthonormal DCT-II */
		double s = (m == 0) ? sqrt(1.0 / KWS_NMEL) : sqrt(2.0 / KWS_NMEL);

		for (k = 0; k < KWS_NMEL; k++) {
			dct[m][k] = (float)(s * cos(M_PI * m * (k + 0.5) / KWS_NMEL));
		}
	}

#ifdef KWS_FE_CMSIS_DSP
	arm_rfft_fast_init_f32(&rfft, KWS_FFT);
	memcpy(tw_cfft, rfft.Sint.pTwiddle, sizeof(tw_cfft));
	memcpy(br_cfft, rfft.Sint.pBitRevTable, sizeof(br_cfft));
	memcpy(tw_rfft, rfft.pTwiddleRFFT, sizeof(tw_rfft));
	rfft.Sint.pTwiddle = tw_cfft;
	rfft.Sint.pBitRevTable = br_cfft;
	rfft.pTwiddleRFFT = tw_rfft;
#else
	for (k = 0; k < NC / 2; k++) {
		tw_c[k] = (float)cos(-2.0 * M_PI * k / NC);
		tw_s[k] = (float)sin(-2.0 * M_PI * k / NC);
	}
	for (k = 0; k < NC; k++) {
		sp_c[k] = (float)cos(-2.0 * M_PI * k / KWS_FFT);
		sp_s[k] = (float)sin(-2.0 * M_PI * k / KWS_FFT);
	}
	int bits = 0;

	while ((1 << bits) < NC) {
		bits++;
	}
	for (k = 0; k < NC; k++) {
		int r = 0;

		for (m = 0; m < bits; m++) {
			r |= ((k >> m) & 1) << (bits - 1 - m);
		}
		bitrev[k] = (uint16_t)r;
	}
#endif
	ready = 1;
}

#ifndef KWS_FE_CMSIS_DSP
/* In-place complex FFT of NC points, interleaved re/im in buf. */
static void cfft(float *x)
{
	int i, len;

	for (i = 0; i < NC; i++) {
		int j = bitrev[i];

		if (j > i) {
			float tr = x[2 * i], ti = x[2 * i + 1];

			x[2 * i] = x[2 * j];
			x[2 * i + 1] = x[2 * j + 1];
			x[2 * j] = tr;
			x[2 * j + 1] = ti;
		}
	}
	for (len = 2; len <= NC; len <<= 1) {
		int half = len >> 1, step = NC / len;

		for (i = 0; i < NC; i += len) {
			for (int k = 0; k < half; k++) {
				float wr = tw_c[k * step], wi = tw_s[k * step];
				float *a = &x[2 * (i + k)], *b = &x[2 * (i + k + half)];
				float br = b[0] * wr - b[1] * wi;
				float bi = b[0] * wi + b[1] * wr;

				b[0] = a[0] - br;
				b[1] = a[1] - bi;
				a[0] += br;
				a[1] += bi;
			}
		}
	}
}

/* Power spectrum of the real signal in buf[0..KWS_FFT) -> pw[0..KWS_NBINS). */
static void power_spectrum(float *pw)
{
	cfft(buf); /* buf viewed as NC complex samples z[n] = x[2n] + j x[2n+1] */
	for (int k = 0; k <= NC; k++) {
		int a = k % NC, b = (NC - k) % NC;
		float zr = buf[2 * a], zi = buf[2 * a + 1];
		float cr = buf[2 * b], ci = -buf[2 * b + 1]; /* conj(Z[NC-k]) */
		float er = 0.5f * (zr + cr), ei = 0.5f * (zi + ci);
		float orr = 0.5f * (zr - cr), oi = 0.5f * (zi - ci);
		/* X[k] = E + W^k * (-j) * O,  W = exp(-j 2 pi / KWS_FFT) */
		float wr = (k < NC) ? sp_c[k] : -1.0f, wi = (k < NC) ? sp_s[k] : 0.0f;
		float tr = oi, ti = -orr; /* -j * O */
		float xr = er + (wr * tr - wi * ti);
		float xi = ei + (wr * ti + wi * tr);

		pw[k] = xr * xr + xi * xi;
	}
}
#else
static void power_spectrum(float *pw)
{
	arm_rfft_fast_f32(&rfft, buf, spec, 0);
	pw[0] = spec[0] * spec[0];
	pw[KWS_NBINS - 1] = spec[1] * spec[1];
	arm_cmplx_mag_squared_f32(&spec[2], &pw[1], KWS_FFT / 2 - 1);
}
#endif

float kws_fe_frame(const int16_t *frame, float *mfcc)
{
	static float pw[KWS_NBINS];
	float mel[KWS_NMEL];
	float energy = 0.0f;
	int k, m;

	if (!ready) {
		kws_fe_init();
	}
	for (k = 0; k < KWS_FRAME_LEN; k++) {
		float s = frame[k] * (1.0f / 32768.0f);

		energy += s * s;
		buf[k] = s * win[k];
	}
	memset(&buf[KWS_FRAME_LEN], 0, sizeof(float) * (KWS_FFT - KWS_FRAME_LEN));
	power_spectrum(pw);

	memset(mel, 0, sizeof(mel));
	for (k = 0; k < KWS_NBINS; k++) {
		int b = mel_lo[k];

		if (b >= 0) {
			mel[b] += pw[k] * mel_wlo[k];
		}
		if (b + 1 < KWS_NMEL && mel_whi[k] > 0.0f) {
			mel[b + 1] += pw[k] * mel_whi[k];
		}
	}
	for (m = 0; m < KWS_NMEL; m++) {
		mel[m] = logf(mel[m] + 1e-6f);
	}
	for (m = 0; m < KWS_NMFCC; m++) {
		float acc = 0.0f;

		for (k = 0; k < KWS_NMEL; k++) {
			acc += dct[m][k] * mel[k];
		}
		mfcc[m] = acc;
	}
	return 10.0f * log10f(energy / KWS_FRAME_LEN + 1e-10f);
}

int kws_fe_clip(const int16_t *pcm, int n, float *out, int max_frames)
{
	int f = 0;

	for (int off = 0; off + KWS_FRAME_LEN <= n && f < max_frames; off += KWS_HOP, f++) {
		(void)kws_fe_frame(&pcm[off], &out[f * KWS_NMFCC]);
	}
	return f;
}
