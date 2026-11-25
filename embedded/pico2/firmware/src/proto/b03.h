/* B-03 v1.1 MPU<->MCU binary protocol (MCU side).
 *
 * Source of truth: orchestrator:src/control_pipeline/codec.py, schemas/bridge.py,
 * schemas/arbiter.py and bridge/mock_mcu.py. Keep this file in sync with them.
 *
 * Wire frame (little-endian):
 *   0 magic 0xAA | 1 type | 2 seq u16 | 4 ack u16 | 6 tick_ms u32 | 10 flags | 11 len
 *   12 payload[len<=40] | 12+len crc16 (CCITT-FALSE over bytes 0..12+len-1)
 * Each frame is COBS-encoded and terminated by 0x00 on the UART.
 *
 * Pure C, no Zephyr dependency: compiled into the firmware and into the host tests.
 */
#ifndef EAA_B03_H
#define EAA_B03_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define B03_MAGIC        0xAAu
#define B03_PROTO_VER    1u
#define B03_HDR_SIZE     12u
#define B03_CRC_SIZE     2u
#define B03_MAX_PAYLOAD  40u
#define B03_MAX_FRAME    (B03_HDR_SIZE + B03_MAX_PAYLOAD + B03_CRC_SIZE) /* 54 */

/* MPU -> MCU */
#define B03_MT_HELLO        0x01u
#define B03_MT_HEARTBEAT    0x02u
#define B03_MT_EVENT_ACK    0x03u
#define B03_MT_RUN          0x10u
#define B03_MT_SET_SPEED    0x11u
#define B03_MT_STOP         0x12u
#define B03_MT_HALT         0x13u
#define B03_MT_CLEAR_FAULT  0x14u
/* MCU -> MPU */
#define B03_MT_HELLO_ACK    0x81u
#define B03_MT_TELEMETRY    0x90u
#define B03_MT_MCU_EVENT    0x91u
#define B03_MT_CMD_ACK      0x92u
#define B03_MT_VOICE_EVENT  0x93u

/* McuState */
enum b03_mcu_state {
	B03_ST_BOOT = 0,
	B03_ST_STOPPED = 1,
	B03_ST_RUNNING = 2,
	B03_ST_SAFE_STOP = 3,
};

/* Header flags, sent in every MCU -> MPU frame */
#define B03_FLAG_ESTOP_CLOSED  (1u << 0)
#define B03_FLAG_PE_CLEAR      (1u << 1)
#define B03_FLAG_HB_VALID      (1u << 2)
#define B03_FLAG_FAULT_LATCHED (1u << 3)
#define B03_FLAG_MOTOR_ENABLED (1u << 4)
#define B03_FLAG_SENSORS_OK    (1u << 5)
#define B03_FLAG_VOICE_ACTIVE  (1u << 6)

/* McuEventKind */
enum b03_event_kind {
	B03_EV_SAFE_STOP = 0x01,
	B03_EV_MOTOR_OVERLOAD = 0x02,
	B03_EV_JAM_DETECTED = 0x03,
	B03_EV_HEARTBEAT_TIMEOUT = 0x04,
	B03_EV_HARDWARE_ESTOP = 0x05,
	B03_EV_ESTOP_RELEASED = 0x06,
	B03_EV_OVERTEMP = 0x07,
	B03_EV_SENSOR_LOSS = 0x08,
	B03_EV_FAULT_CLEARED = 0x09,
	B03_EV_STATE_CHANGED = 0x0A,
	B03_EV_BOOT = 0x0B,
	B03_EV_VOICE_WAKE = 0x0C,
	B03_EV_VOICE_STOP = 0x0D,
};

/* ReasonCode, MCU range 0x00..0x0F */
enum b03_reason {
	B03_R_OK = 0x00,
	B03_R_ESTOP_ACTIVE = 0x01,
	B03_R_PHOTOELECTRIC_BLOCKED = 0x02,
	B03_R_MOTOR_OVERCURRENT = 0x03,
	B03_R_MOTOR_OVERTEMP = 0x04,
	B03_R_HEARTBEAT_TIMEOUT = 0x05,
	B03_R_STATE_NOT_PERMITTED = 0x06,
	B03_R_SENSOR_LOSS = 0x07,
	B03_R_BAD_PARAM = 0x08,
	B03_R_EXPIRED = 0x09,
	B03_R_BUSY = 0x0A,
	B03_R_UNKNOWN_CMD = 0x0B,
	B03_R_LIMIT_EXCEEDED = 0x0C,
	B03_R_MPU_HALT_REQUEST = 0x0D,
	B03_R_FAULT_LATCHED = 0x0E,
	B03_R_NOT_SYNCED = 0x0F,
};

/* CMD_ACK result */
#define B03_ACK_ACCEPTED  0u
#define B03_ACK_REJECTED  1u
#define B03_ACK_DUPLICATE 2u

/* VOICE_EVENT kind */
#define B03_VOICE_WAKE 0u
#define B03_VOICE_STOP 1u

/* temp_c10 special values */
#define B03_TEMP_NOT_INSTALLED 0x7FFF
#define B03_TEMP_FAULT         0x7FFE

struct b03_frame {
	uint8_t type;
	uint16_t seq;
	uint16_t ack;
	uint32_t tick_ms;
	uint8_t flags;
	uint8_t len;
	uint8_t payload[B03_MAX_PAYLOAD];
};

enum b03_err {
	B03_OK = 0,
	B03_E_SHORT = -1,
	B03_E_MAGIC = -2,
	B03_E_LEN = -3,
	B03_E_CRC = -4,
	B03_E_SPACE = -5,
};

/* Encode a frame into out (cap >= 12 + len + 2). Returns the length, or < 0. */
int b03_encode(const struct b03_frame *f, uint8_t *out, size_t cap);
/* Decode and validate. Trailing bytes after the CRC are ignored (same as codec.py). */
int b03_decode(const uint8_t *in, size_t n, struct b03_frame *f);

/* Parsed MPU -> MCU message */
struct b03_cmd {
	uint8_t type;
	uint16_t cmd_id;     /* RUN / SET_SPEED / STOP / HALT / CLEAR_FAULT */
	uint16_t ttl_ms;
	uint8_t kind;        /* RUN: 0 start, 1 resume. STOP: 0 stop, 1 pause */
	uint16_t setpoint_mms;
	uint8_t proto_ver;   /* HELLO */
	uint32_t session_id; /* HELLO */
	uint8_t mpu_state;   /* HEARTBEAT */
	uint16_t event_seq;  /* EVENT_ACK */
};

/* Returns 0 on success, B03_E_LEN when the payload is too short,
 * or B03_R_UNKNOWN_CMD (a positive value) for an unknown message type. */
int b03_parse(const struct b03_frame *f, struct b03_cmd *c);

/* Payload builders (MCU -> MPU). Each one sets f->type and f->len. */
void b03_pl_hello_ack(struct b03_frame *f, uint16_t boot_id, uint16_t fw_ver,
		      uint8_t state, uint8_t fault_reason);
void b03_pl_telemetry(struct b03_frame *f, uint8_t state, uint8_t fault_reason,
		      uint16_t setpoint_mms, uint16_t speed_mms, uint16_t current_ma,
		      int16_t temp_c10, uint8_t sensor_status, uint16_t pwm_pm);
void b03_pl_mcu_event(struct b03_frame *f, uint8_t kind, uint8_t reason, uint16_t event_seq,
		      uint8_t state, int16_t observed, int16_t threshold);
void b03_pl_cmd_ack(struct b03_frame *f, uint16_t cmd_id, uint8_t result, uint8_t reason,
		    uint8_t state);
void b03_pl_voice_event(struct b03_frame *f, uint8_t kind, uint8_t confidence_pct,
			uint16_t event_seq, uint8_t state, uint16_t model_ver);

/* Same rule as codec.newer_seq(). */
static inline bool b03_newer_seq(uint16_t a, uint16_t b)
{
	uint16_t d = (uint16_t)(a - b);

	return d != 0 && d < 0x8000u;
}

#endif
