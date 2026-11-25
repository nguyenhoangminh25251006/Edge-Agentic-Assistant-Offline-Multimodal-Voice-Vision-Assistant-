#ifndef EAA_AUDIO_OUT_H
#define EAA_AUDIO_OUT_H
#include <stddef.h>
#include <stdint.h>

int audio_out_init(void);
void audio_out_begin(uint16_t event_seq, uint16_t sample_rate);
void audio_out_samples(const int16_t *s, size_t n);
void audio_out_end(uint32_t total);
uint32_t audio_out_dropped(void);

#endif
