/* Consistent Overhead Byte Stuffing, 0x00 is the frame delimiter on the wire.
 * Byte-compatible with cobs_encode()/cobs_decode() in
 * orchestrator:src/control_pipeline/bridge/transport.py (PipeTransport, Profile B). */
#ifndef EAA_COBS_H
#define EAA_COBS_H
#include <stddef.h>
#include <stdint.h>

/* Worst-case encoded size for n input bytes (without the 0x00 delimiter). */
#define COBS_MAX_ENCODED(n) ((n) + ((n) / 254u) + 1u)

/* Encode len bytes. out must hold COBS_MAX_ENCODED(len). Returns encoded length
 * (no trailing delimiter). */
size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out);

/* Decode one frame (delimiter already stripped). Returns decoded length,
 * or -1 if malformed or larger than cap. */
int cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t cap);

#endif
