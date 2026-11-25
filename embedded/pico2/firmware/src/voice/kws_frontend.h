/* Wake-word audio front end: 16 kHz int16 PCM -> MFCC frames.
 *
 * Plain C99, no Zephyr, no heap: the SAME file is compiled into the firmware
 * (Pico 2 / STM32U585) and into a host shared library that the training
 * scripts call through ctypes (embedded/kws/), so the model is trained on
 * exactly the features the MCU computes.
 *
 *   frame 30 ms (480 samples), hop 20 ms (320), Hann window, 512-point FFT,
 *   40 mel bands 20..8000 Hz (HTK scale), log, DCT-II -> 10 MFCC.
 *   1 s model window = 49 frames x 10 coefficients.
 */
#ifndef EAA_KWS_FRONTEND_H
#define EAA_KWS_FRONTEND_H
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KWS_SR        16000
#define KWS_FRAME_LEN 480
#define KWS_HOP       320
#define KWS_FFT       512
#define KWS_NBINS     (KWS_FFT / 2 + 1)
#define KWS_NMEL      40
#define KWS_NMFCC     10
#define KWS_NFRAMES   49 /* 1 + (16000 - 480) / 320 */
#define KWS_WINDOW    16000

#if defined(_WIN32) && defined(KWS_FE_DLL)
#define KWS_FE_API __declspec(dllexport)
#else
#define KWS_FE_API
#endif

/* Builds the window, twiddle, mel and DCT tables (about 9 KB of RAM). */
KWS_FE_API void kws_fe_init(void);
/* One frame of KWS_FRAME_LEN samples -> KWS_NMFCC coefficients.
 * Returns the frame energy in dBFS (used by the voice activity gate). */
KWS_FE_API float kws_fe_frame(const int16_t *frame, float *mfcc);
/* Whole clip -> frames x KWS_NMFCC (row major). Returns the frame count. */
KWS_FE_API int kws_fe_clip(const int16_t *pcm, int n, float *out, int max_frames);

#ifdef __cplusplus
}
#endif
#endif
