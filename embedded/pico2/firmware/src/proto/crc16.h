/* CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR).
 * Matches crc16_ccitt_false() in orchestrator:src/control_pipeline/codec.py. */
#ifndef EAA_CRC16_H
#define EAA_CRC16_H
#include <stddef.h>
#include <stdint.h>

#define CRC16_INIT 0xFFFFu

uint16_t crc16_ccitt_false(const uint8_t *data, size_t len, uint16_t crc);

#endif
