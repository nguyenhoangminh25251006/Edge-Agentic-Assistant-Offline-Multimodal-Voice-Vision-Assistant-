/* Host test of the firmware protocol code (src/proto). Builds with any C compiler:
 *   cc -I../../src -o b03_host_test b03_host_test.c ../../src/proto/crc16.c ../../src/proto/cobs.c ../../src/proto/b03.c
 * Modes:
 *   (none)   self tests, exit code 0 = pass
 *   enc      print MCU->MPU frames and COBS vectors as hex (checked by cross_check.py)
 *   dec      read hex MPU->MCU frames from stdin, print the parsed fields
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "proto/b03.h"
#include "proto/cobs.h"
#include "proto/crc16.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static int unhex(const char *s, uint8_t *out, size_t cap)
{
	size_t n = 0;
	unsigned v;

	while (*s && n < cap) {
		while (*s == ' ') s++;
		if (!*s || *s == '\n' || *s == '\r') break;
		if (sscanf(s, "%2x", &v) != 1) break;
		out[n++] = (uint8_t)v;
		s += 2;
	}
	return (int)n;
}

static void hex(const char *name, const uint8_t *b, size_t n)
{
	printf("%s ", name);
	for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
	printf("\n");
}

static void emit(const char *name, struct b03_frame *f, uint16_t seq, uint16_t ack, uint32_t tick, uint8_t flags)
{
	uint8_t out[B03_MAX_FRAME];

	f->seq = seq; f->ack = ack; f->tick_ms = tick; f->flags = flags;
	int n = b03_encode(f, out, sizeof(out));
	hex(name, out, (size_t)n);
}

static void mode_enc(void)
{
	struct b03_frame f;

	b03_pl_hello_ack(&f, 0x1234, 0x0100, B03_ST_STOPPED, 0);
	emit("hello_ack", &f, 1, 7, 1500, 0x27);
	b03_pl_telemetry(&f, B03_ST_RUNNING, 0, 120, 118, 850, 342, 0x00, 1000 - 1);
	emit("telemetry", &f, 40, 2, 5000, 0x37);
	b03_pl_telemetry(&f, B03_ST_SAFE_STOP, 0x03, 300, 0, 3000, -125, 0x05, 0);
	emit("telemetry_neg", &f, 0xFFFF, 0xFFFE, 0xFFFFFFFF, 0x2D);
	b03_pl_mcu_event(&f, B03_EV_MOTOR_OVERLOAD, B03_R_MOTOR_OVERCURRENT, 9, B03_ST_SAFE_STOP, 3000, 2500);
	emit("mcu_event", &f, 3, 4, 77, 0x2D);
	b03_pl_mcu_event(&f, B03_EV_HEARTBEAT_TIMEOUT, B03_R_HEARTBEAT_TIMEOUT, 0x0100, B03_ST_SAFE_STOP, -1, 300);
	emit("mcu_event2", &f, 0, 0, 0, 0);
	b03_pl_cmd_ack(&f, 0xBEEF, B03_ACK_REJECTED, B03_R_FAULT_LATCHED, B03_ST_SAFE_STOP);
	emit("cmd_ack", &f, 10, 11, 12, 0x08);
	b03_pl_voice_event(&f, B03_VOICE_STOP, 93, 0x0201, B03_ST_STOPPED, 0x0001);
	emit("voice_event", &f, 21, 22, 23, 0x67);

	/* COBS vectors */
	static const uint8_t v1[] = { 0x00 };
	static const uint8_t v2[] = { 0x11, 0x00, 0x22, 0x00, 0x00, 0x33 };
	uint8_t v3[300];
	uint8_t enc[400];
	size_t n;

	for (int i = 0; i < 300; i++) v3[i] = (uint8_t)((i % 255) + 1);
	n = cobs_encode(v1, sizeof(v1), enc); hex("cobs1", enc, n);
	n = cobs_encode(v2, sizeof(v2), enc); hex("cobs2", enc, n);
	n = cobs_encode(v3, 254, enc); hex("cobs3", enc, n);
	n = cobs_encode(v3, 300, enc); hex("cobs4", enc, n);
	n = cobs_encode(v3, 0, enc); hex("cobs5", enc, n);
}

static void mode_dec(void)
{
	char line[512];

	while (fgets(line, sizeof(line), stdin)) {
		uint8_t raw[128];
		struct b03_frame f;
		struct b03_cmd c;
		int n = unhex(line, raw, sizeof(raw));
		int rc = b03_decode(raw, (size_t)n, &f);

		if (rc != B03_OK) { printf("decode_err %d\n", rc); continue; }
		rc = b03_parse(&f, &c);
		printf("type=%u seq=%u ack=%u tick=%u flags=%u rc=%d cmd_id=%u ttl=%u kind=%u sp=%u proto=%u session=%u mpu_state=%u event_seq=%u\n",
		       f.type, f.seq, f.ack, (unsigned)f.tick_ms, f.flags, rc, c.cmd_id, c.ttl_ms, c.kind,
		       c.setpoint_mms, c.proto_ver, (unsigned)c.session_id, c.mpu_state, c.event_seq);
	}
}

static void self_tests(void)
{
	/* CRC-16/CCITT-FALSE check value */
	CHECK(crc16_ccitt_false((const uint8_t *)"123456789", 9, CRC16_INIT) == 0x29B1);

	/* Golden TELEMETRY vector, B-03 spec section 9 (codec.py __main__) */
	uint8_t g[64];
	struct b03_frame f;
	int n = unhex("aa90280002008813000037 0d0200780076005203560180760017 6f", g, sizeof(g));

	CHECK(n == 27);
	CHECK(b03_decode(g, (size_t)n, &f) == B03_OK);
	CHECK(f.type == B03_MT_TELEMETRY && f.flags == 0x37 && f.len == 13);
	CHECK(f.payload[0] == B03_ST_RUNNING);
	CHECK((f.payload[2] | f.payload[3] << 8) == 120);
	CHECK((f.payload[4] | f.payload[5] << 8) == 118);
	CHECK((f.payload[6] | f.payload[7] << 8) == 850);
	CHECK((f.payload[8] | f.payload[9] << 8) == 342);
	/* Corrupt one byte -> CRC error, wrong magic, short */
	g[14] ^= 1;
	CHECK(b03_decode(g, (size_t)n, &f) == B03_E_CRC);
	g[14] ^= 1;
	g[0] = 0x55;
	CHECK(b03_decode(g, (size_t)n, &f) == B03_E_MAGIC);
	g[0] = 0xAA;
	CHECK(b03_decode(g, 13, &f) == B03_E_SHORT);
	CHECK(b03_decode(g, 20, &f) == B03_E_LEN);

	/* COBS round trip, all lengths 0..300 with zeros sprinkled in */
	for (int len = 0; len <= 300; len++) {
		uint8_t in[300], enc[COBS_MAX_ENCODED(300)], dec[300];

		for (int i = 0; i < len; i++) in[i] = (uint8_t)((i * 7 + len) % 5 == 0 ? 0 : i * 13 + 1);
		size_t e = cobs_encode(in, (size_t)len, enc);
		int bad = 0;

		for (size_t i = 0; i < e; i++) bad |= enc[i] == 0;
		CHECK(!bad);
		CHECK(e <= COBS_MAX_ENCODED((size_t)len));
		int d = cobs_decode(enc, e, dec, sizeof(dec));

		CHECK(d == len);
		CHECK(d < 0 || memcmp(in, dec, (size_t)len) == 0);
	}
	uint8_t junk[3] = { 5, 1, 2 };
	uint8_t out[8];

	CHECK(cobs_decode(junk, 3, out, sizeof(out)) == -1); /* code points past the end */
	CHECK(cobs_decode((const uint8_t *)"\x03\x01\x02", 3, out, 1) == -1); /* too big */

	/* encode/parse round trip of an MPU command built by hand */
	struct b03_frame r = { .type = B03_MT_RUN, .seq = 2, .ack = 1, .tick_ms = 2000, .len = 7,
			       .payload = { 1, 0, 0xF4, 1, 0, 0x2C, 1 } };
	uint8_t buf[B03_MAX_FRAME];
	struct b03_cmd c;

	n = b03_encode(&r, buf, sizeof(buf));
	CHECK(n == 21);
	CHECK(b03_decode(buf, (size_t)n, &f) == B03_OK);
	CHECK(b03_parse(&f, &c) == 0);
	CHECK(c.cmd_id == 1 && c.ttl_ms == 500 && c.kind == 0 && c.setpoint_mms == 300);
	f.len = 6;
	CHECK(b03_parse(&f, &c) == B03_E_LEN);
	f.type = 0x7E;
	CHECK(b03_parse(&f, &c) == B03_R_UNKNOWN_CMD);
	CHECK(b03_newer_seq(1, 0xFFFF) && !b03_newer_seq(5, 5) && !b03_newer_seq(0x8005, 5));

	printf(fails ? "SELF TESTS FAILED: %d\n" : "self tests passed\n", fails);
}

int main(int argc, char **argv)
{
	if (argc > 1 && !strcmp(argv[1], "enc")) { mode_enc(); return 0; }
	if (argc > 1 && !strcmp(argv[1], "dec")) { mode_dec(); return 0; }
	self_tests();
	return fails ? 1 : 0;
}
