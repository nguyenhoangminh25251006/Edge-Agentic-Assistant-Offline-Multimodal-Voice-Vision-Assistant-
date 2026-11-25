#include "b03.h"
#include "crc16.h"

static void put16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
}

static void put32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
	p[2] = (uint8_t)(v >> 16);
	p[3] = (uint8_t)(v >> 24);
}

static uint16_t get16(const uint8_t *p)
{
	return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t get32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
	       ((uint32_t)p[3] << 24);
}

int b03_encode(const struct b03_frame *f, uint8_t *out, size_t cap)
{
	if (f->len > B03_MAX_PAYLOAD) {
		return B03_E_LEN;
	}
	size_t total = B03_HDR_SIZE + f->len + B03_CRC_SIZE;

	if (cap < total) {
		return B03_E_SPACE;
	}
	out[0] = B03_MAGIC;
	out[1] = f->type;
	put16(&out[2], f->seq);
	put16(&out[4], f->ack);
	put32(&out[6], f->tick_ms);
	out[10] = f->flags;
	out[11] = f->len;
	for (uint8_t i = 0; i < f->len; i++) {
		out[B03_HDR_SIZE + i] = f->payload[i];
	}
	put16(&out[B03_HDR_SIZE + f->len],
	      crc16_ccitt_false(out, B03_HDR_SIZE + f->len, CRC16_INIT));
	return (int)total;
}

int b03_decode(const uint8_t *in, size_t n, struct b03_frame *f)
{
	if (n < B03_HDR_SIZE + B03_CRC_SIZE) {
		return B03_E_SHORT;
	}
	if (in[0] != B03_MAGIC) {
		return B03_E_MAGIC;
	}
	uint8_t len = in[11];

	if (len > B03_MAX_PAYLOAD || n < (size_t)(B03_HDR_SIZE + len + B03_CRC_SIZE)) {
		return B03_E_LEN;
	}
	if (crc16_ccitt_false(in, B03_HDR_SIZE + len, CRC16_INIT) !=
	    get16(&in[B03_HDR_SIZE + len])) {
		return B03_E_CRC;
	}
	f->type = in[1];
	f->seq = get16(&in[2]);
	f->ack = get16(&in[4]);
	f->tick_ms = get32(&in[6]);
	f->flags = in[10];
	f->len = len;
	for (uint8_t i = 0; i < len; i++) {
		f->payload[i] = in[B03_HDR_SIZE + i];
	}
	return B03_OK;
}

int b03_parse(const struct b03_frame *f, struct b03_cmd *c)
{
	const uint8_t *p = f->payload;
	uint8_t need;

	*c = (struct b03_cmd){ .type = f->type };
	switch (f->type) {
	case B03_MT_HELLO:       need = 5; break;
	case B03_MT_HEARTBEAT:   need = 1; break;
	case B03_MT_EVENT_ACK:   need = 2; break;
	case B03_MT_RUN:         need = 7; break;
	case B03_MT_SET_SPEED:   need = 6; break;
	case B03_MT_STOP:        need = 5; break;
	case B03_MT_HALT:
	case B03_MT_CLEAR_FAULT: need = 4; break;
	default:
		return B03_R_UNKNOWN_CMD;
	}
	if (f->len < need) {
		return B03_E_LEN;
	}

	switch (f->type) {
	case B03_MT_HELLO: /* proto_ver u8, session_id u32 */
		c->proto_ver = p[0];
		c->session_id = get32(&p[1]);
		break;
	case B03_MT_HEARTBEAT: /* mpu_state u8 */
		c->mpu_state = p[0];
		break;
	case B03_MT_EVENT_ACK: /* event_seq u16 */
		c->event_seq = get16(&p[0]);
		break;
	case B03_MT_RUN: /* cmd_id u16, ttl u16, kind u8, setpoint u16 */
		c->cmd_id = get16(&p[0]);
		c->ttl_ms = get16(&p[2]);
		c->kind = p[4];
		c->setpoint_mms = get16(&p[5]);
		break;
	case B03_MT_SET_SPEED: /* cmd_id u16, ttl u16, setpoint u16 */
		c->cmd_id = get16(&p[0]);
		c->ttl_ms = get16(&p[2]);
		c->setpoint_mms = get16(&p[4]);
		break;
	case B03_MT_STOP: /* cmd_id u16, ttl u16, kind u8 */
		c->cmd_id = get16(&p[0]);
		c->ttl_ms = get16(&p[2]);
		c->kind = p[4];
		break;
	default: /* HALT, CLEAR_FAULT: cmd_id u16, ttl u16 */
		c->cmd_id = get16(&p[0]);
		c->ttl_ms = get16(&p[2]);
		break;
	}
	return 0;
}

void b03_pl_hello_ack(struct b03_frame *f, uint16_t boot_id, uint16_t fw_ver,
		      uint8_t state, uint8_t fault_reason)
{
	uint8_t *p = f->payload;

	f->type = B03_MT_HELLO_ACK;
	p[0] = B03_PROTO_VER;
	put16(&p[1], boot_id);
	put16(&p[3], fw_ver);
	p[5] = state;
	p[6] = fault_reason;
	f->len = 7;
}

void b03_pl_telemetry(struct b03_frame *f, uint8_t state, uint8_t fault_reason,
		      uint16_t setpoint_mms, uint16_t speed_mms, uint16_t current_ma,
		      int16_t temp_c10, uint8_t sensor_status, uint16_t pwm_pm)
{
	uint8_t *p = f->payload;

	f->type = B03_MT_TELEMETRY;
	p[0] = state;
	p[1] = fault_reason;
	put16(&p[2], setpoint_mms);
	put16(&p[4], speed_mms);
	put16(&p[6], current_ma);
	put16(&p[8], (uint16_t)temp_c10);
	p[10] = sensor_status;
	put16(&p[11], pwm_pm);
	f->len = 13;
}

void b03_pl_mcu_event(struct b03_frame *f, uint8_t kind, uint8_t reason, uint16_t event_seq,
		      uint8_t state, int16_t observed, int16_t threshold)
{
	uint8_t *p = f->payload;

	f->type = B03_MT_MCU_EVENT;
	p[0] = kind;
	p[1] = reason;
	put16(&p[2], event_seq);
	p[4] = state;
	put16(&p[5], (uint16_t)observed);
	put16(&p[7], (uint16_t)threshold);
	f->len = 9;
}

void b03_pl_cmd_ack(struct b03_frame *f, uint16_t cmd_id, uint8_t result, uint8_t reason,
		    uint8_t state)
{
	uint8_t *p = f->payload;

	f->type = B03_MT_CMD_ACK;
	put16(&p[0], cmd_id);
	p[2] = result;
	p[3] = reason;
	p[4] = state;
	f->len = 5;
}

void b03_pl_voice_event(struct b03_frame *f, uint8_t kind, uint8_t confidence_pct,
			uint16_t event_seq, uint8_t state, uint16_t model_ver)
{
	uint8_t *p = f->payload;

	f->type = B03_MT_VOICE_EVENT;
	p[0] = kind;
	p[1] = confidence_pct;
	put16(&p[2], event_seq);
	p[4] = state;
	put16(&p[5], model_ver);
	f->len = 7;
}
