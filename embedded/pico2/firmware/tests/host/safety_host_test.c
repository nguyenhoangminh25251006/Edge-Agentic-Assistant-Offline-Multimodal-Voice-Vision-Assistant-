/* Host unit test of the MCU safety state machine (src/safety.c) with a Zephyr shim.
 *   cc -std=c11 -Ishim -I../../src -o safety_host_test safety_host_test.c ../../src/proto/b03.c ../../src/proto/crc16.c
 * Covers the fault-injection list of CH-241 at logic level (no hardware). */
#include <stdio.h>
#include "../../src/safety.c"

int64_t shim_now_ms;
static struct eaa_sensors fake;
static int ev_n;
static uint8_t ev_kind[32], ev_reason[32];
static int fails;

#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); fails++; } } while (0)

/* --- stubs of the other modules --- */
void sensors_get(struct eaa_sensors *out) { *out = fake; }
uint16_t control_speed_mms(void) { return 0; }
void bridge_emit_event(uint8_t kind, uint8_t reason, int16_t o, int16_t t)
{
	(void)o; (void)t;
	if (ev_n < 32) { ev_kind[ev_n] = kind; ev_reason[ev_n] = reason; ev_n++; }
}

static bool has_event(uint8_t kind, uint8_t reason)
{
	for (int i = 0; i < ev_n; i++) {
		if (ev_kind[i] == kind && ev_reason[i] == reason) return true;
	}
	return false;
}

static uint16_t next_id = 1;
static uint8_t cmd(uint8_t type, uint16_t sp, uint16_t ttl, uint8_t *reason)
{
	struct b03_cmd c = { .type = type, .cmd_id = next_id++, .ttl_ms = ttl, .setpoint_mms = sp };
	return safety_handle_cmd(&c, reason);
}

static void tick(int ms) /* advance time like the 10 ms safety thread, with heartbeats off */
{
	for (int t = 0; t < ms; t += 10) {
		shim_now_ms += 10;
		check_invariants(&fake, shim_now_ms);
		flush_events();
	}
}

static void tick_hb(int ms) /* same, MPU heartbeat every 100 ms */
{
	for (int t = 0; t < ms; t += 10) {
		shim_now_ms += 10;
		if (shim_now_ms % 100 == 0) safety_on_heartbeat();
		check_invariants(&fake, shim_now_ms);
		flush_events();
	}
}

static uint8_t st(void) { struct eaa_status s; safety_get_status(&s); return s.state; }
static uint8_t flags(void) { struct eaa_status s; safety_get_status(&s); return s.flags; }

static void healthy(void)
{
	fake = (struct eaa_sensors){ .estop_open = false, .pe_blocked = false, .current_ok = true,
				     .temp_ok = true, .current_ma = 800, .temp_c10 = 350 };
}

static void reset_to_running(uint16_t sp)
{
	uint8_t r;

	healthy();
	safety_on_heartbeat();
	tick_hb(50);
	if (st() == B03_ST_SAFE_STOP) CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_ACCEPTED);
	CHECK(cmd(B03_MT_RUN, sp, 500, &r) == B03_ACK_ACCEPTED);
	CHECK(st() == B03_ST_RUNNING);
	ev_n = 0;
}

int main(void)
{
	uint8_t r;

	healthy();
	safety_init();
	CHECK(st() == B03_ST_STOPPED);

	/* 1. No HELLO yet -> NOT_SYNCED */
	CHECK(cmd(B03_MT_RUN, 200, 500, &r) == B03_ACK_REJECTED && r == B03_R_NOT_SYNCED);

	/* 2. HELLO, RUN accepted, duplicate cmd_id */
	safety_on_hello();
	struct b03_cmd run_cmd = { .type = B03_MT_RUN, .cmd_id = 777, .ttl_ms = 500, .setpoint_mms = 200 };
	CHECK(safety_handle_cmd(&run_cmd, &r) == B03_ACK_ACCEPTED && st() == B03_ST_RUNNING);
	CHECK(flags() & B03_FLAG_MOTOR_ENABLED);
	CHECK(safety_handle_cmd(&run_cmd, &r) == B03_ACK_DUPLICATE);

	/* 3. Parameter checks */
	CHECK(cmd(B03_MT_SET_SPEED, 600, 500, &r) == B03_ACK_REJECTED && r == B03_R_LIMIT_EXCEEDED);
	CHECK(cmd(B03_MT_SET_SPEED, 10, 500, &r) == B03_ACK_REJECTED && r == B03_R_BAD_PARAM);
	CHECK(cmd(B03_MT_SET_SPEED, 300, 0, &r) == B03_ACK_REJECTED && r == B03_R_EXPIRED);
	CHECK(cmd(B03_MT_SET_SPEED, 300, 500, &r) == B03_ACK_ACCEPTED);
	CHECK(cmd(0x7E, 0, 500, &r) == B03_ACK_REJECTED && r == B03_R_UNKNOWN_CMD);

	/* 4. Heartbeat lost while running -> SAFE_STOP + HEARTBEAT_TIMEOUT */
	ev_n = 0;
	tick(290);
	CHECK(st() == B03_ST_RUNNING);
	tick(30);
	CHECK(st() == B03_ST_SAFE_STOP);
	CHECK(has_event(B03_EV_HEARTBEAT_TIMEOUT, B03_R_HEARTBEAT_TIMEOUT));
	CHECK(!(flags() & B03_FLAG_HB_VALID) && (flags() & B03_FLAG_FAULT_LATCHED));
	/* RUN refused while latched, CLEAR refused while heartbeat is still missing */
	CHECK(cmd(B03_MT_RUN, 200, 500, &r) == B03_ACK_REJECTED);
	CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_REJECTED && r == B03_R_HEARTBEAT_TIMEOUT);
	/* STOP is always accepted */
	CHECK(cmd(B03_MT_STOP, 0, 500, &r) == B03_ACK_ACCEPTED && st() == B03_ST_SAFE_STOP);
	/* heartbeat back -> CLEAR_FAULT -> STOPPED + FAULT_CLEARED */
	ev_n = 0;
	safety_on_heartbeat();
	CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_ACCEPTED && st() == B03_ST_STOPPED);
	CHECK(has_event(B03_EV_FAULT_CLEARED, B03_R_OK));

	/* 5. Heartbeat lost while STOPPED: flag only, no trip */
	tick(400);
	CHECK(st() == B03_ST_STOPPED && !(flags() & B03_FLAG_HB_VALID));
	CHECK(cmd(B03_MT_RUN, 200, 500, &r) == B03_ACK_REJECTED && r == B03_R_HEARTBEAT_TIMEOUT);

	/* 6. E-stop: trip, release event, stays latched until CLEAR */
	reset_to_running(200);
	fake.estop_open = true;
	tick_hb(10);
	CHECK(st() == B03_ST_SAFE_STOP && has_event(B03_EV_HARDWARE_ESTOP, B03_R_ESTOP_ACTIVE));
	CHECK(!(flags() & B03_FLAG_ESTOP_CLOSED));
	CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_REJECTED && r == B03_R_ESTOP_ACTIVE);
	fake.estop_open = false;
	tick_hb(10);
	CHECK(has_event(B03_EV_ESTOP_RELEASED, B03_R_OK) && st() == B03_ST_SAFE_STOP);
	CHECK(cmd(B03_MT_RUN, 200, 500, &r) == B03_ACK_REJECTED && r == B03_R_FAULT_LATCHED);
	CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_ACCEPTED && st() == B03_ST_STOPPED);

	/* 7. Overcurrent: start-up spike filtered, sustained overload trips */
	reset_to_running(200);
	fake.current_ma = 3000;
	tick_hb(40);
	CHECK(st() == B03_ST_RUNNING);
	fake.current_ma = 900;
	tick_hb(20);
	fake.current_ma = 3000;
	tick_hb(80);
	CHECK(st() == B03_ST_SAFE_STOP && has_event(B03_EV_MOTOR_OVERLOAD, B03_R_MOTOR_OVERCURRENT));
	CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_REJECTED && r == B03_R_MOTOR_OVERCURRENT);

	/* 8. Overtemperature */
	reset_to_running(200);
	fake.temp_c10 = 815;
	tick_hb(10);
	CHECK(st() == B03_ST_SAFE_STOP && has_event(B03_EV_OVERTEMP, B03_R_MOTOR_OVERTEMP));

	/* 9. Jam: beam blocked for 3 s while running (short blocks are products passing) */
	reset_to_running(200);
	fake.pe_blocked = true;
	tick_hb(1000);
	fake.pe_blocked = false;
	tick_hb(100);
	CHECK(st() == B03_ST_RUNNING);
	fake.pe_blocked = true;
	tick_hb(3100);
	CHECK(st() == B03_ST_SAFE_STOP && has_event(B03_EV_JAM_DETECTED, B03_R_PHOTOELECTRIC_BLOCKED));
	fake.pe_blocked = false;

	/* 10. Sensor loss while running */
	reset_to_running(200);
	fake.current_ok = false;
	tick_hb(10);
	CHECK(st() == B03_ST_SAFE_STOP && has_event(B03_EV_SENSOR_LOSS, B03_R_SENSOR_LOSS));
	CHECK(cmd(B03_MT_CLEAR_FAULT, 0, 500, &r) == B03_ACK_REJECTED && r == B03_R_SENSOR_LOSS);
	healthy();

	/* 11. HALT from the MPU: immediate, latched */
	reset_to_running(200);
	CHECK(cmd(B03_MT_HALT, 0, 500, &r) == B03_ACK_ACCEPTED && st() == B03_ST_SAFE_STOP);
	struct eaa_status s;
	safety_get_status(&s);
	CHECK(s.fault_reason == B03_R_MPU_HALT_REQUEST);
	bool run, hard;
	uint16_t sp;
	safety_target(&run, &sp, &hard);
	CHECK(!run && hard);

	/* 12. Spoken "stop": controlled stop by the MCU, no MPU involved */
	reset_to_running(250);
	safety_set_voice_active(true);
	CHECK(flags() & B03_FLAG_VOICE_ACTIVE);
	safety_voice_stop();
	CHECK(st() == B03_ST_STOPPED && !(flags() & B03_FLAG_VOICE_ACTIVE));
	safety_target(&run, &sp, &hard);
	CHECK(!run && !hard); /* ramp down, not a hard cut */

	/* 13. Boot with the E-stop pressed starts latched */
	healthy();
	fake.estop_open = true;
	fault_latched = false;
	safety_init();
	CHECK(st() == B03_ST_SAFE_STOP);

	printf(fails ? "SAFETY TESTS FAILED: %d\n" : "safety tests passed (13 scenarios)\n", fails);
	return fails ? 1 : 0;
}
