/* Microphone capture interface (16 kHz, mono, int16).
 * Pico 2: voice/i2s_in_rp2.c (PIO I2S master, INMP441-type mic).
 * STM32U585 / UNO Q: implement the same four functions with the Zephyr I2S (SAI)
 * or DMIC API; nothing else in the firmware changes. */
#ifndef EAA_AUDIO_IN_H
#define EAA_AUDIO_IN_H
#include <stddef.h>
#include <stdint.h>
#include <zephyr/kernel.h>

int audio_in_init(uint32_t sample_rate);
/* Blocks up to `timeout`, returns the number of samples copied (0 on timeout). */
size_t audio_in_read(int16_t *buf, size_t max, k_timeout_t timeout);
uint32_t audio_in_overruns(void);

#endif
