#include "cobs.h"

size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out)
{
	size_t code_idx = 0;
	size_t w = 1;
	uint8_t code = 1;

	for (size_t r = 0; r < len; r++) {
		if (in[r] == 0x00) {
			out[code_idx] = code;
			code = 1;
			code_idx = w++;
		} else {
			out[w++] = in[r];
			code++;
			if (code == 0xFF) {
				out[code_idx] = code;
				code = 1;
				code_idx = w++;
			}
		}
	}
	out[code_idx] = code;
	return w;
}

int cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t cap)
{
	size_t r = 0;
	size_t w = 0;

	while (r < len) {
		uint8_t code = in[r];

		if (code == 0x00) {
			return -1;
		}
		r++;
		if (r + code - 1u > len) {
			return -1;
		}
		for (uint8_t i = 1; i < code; i++) {
			if (w >= cap) {
				return -1;
			}
			out[w++] = in[r++];
		}
		if (code < 0xFF && r < len) {
			if (w >= cap) {
				return -1;
			}
			out[w++] = 0x00;
		}
	}
	return (int)w;
}
