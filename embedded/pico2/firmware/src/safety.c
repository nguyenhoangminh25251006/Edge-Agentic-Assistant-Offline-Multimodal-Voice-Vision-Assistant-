/* MCU safety state machine: BOOT -> STOPPED <-> RUNNING, any -> SAFE_STOP (latched).
 *
 * The MPU (orchestrator FSM + Safety Arbiter) proposes; this file decides.
 * Every command is validated again here, and every invariant is checked on the
 * MCU so the belt stops even if the MPU is slow, wrong or frozen.
 *
 * Invariants (tripped -> SAFE_STOP, fault latched until CLEAR_FAULT once the cause is gone):
 *   E-stop open                 -> HARDWARE_ESTOP / ESTOP_ACTIVE
 *   no heartbeat (300 ms) while running -> HEARTBEAT_TIMEOUT
 *   current > limit (filtered)  -> MOTOR_OVERLOAD / MOTOR_OVERCURRENT
 *   temperature > limit         -> OVERTEMP / MOTOR_OVERTEMP
 *   beam blocked too long while running -> JAM_DETECTED / PHOTOELECTRIC_BLOCKED
 *   required sensor lost while running  -> SENSOR_LOSS
 * Behaviour mirrors orchestrator:src/control_pipeline/bridge/mock_mcu.py.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/logging/log.h>
#include "eaa.h"

LOG_MODULE_REGISTER(safety, LOG_LEVEL_INF);

#define TICK_MS 10
#define RECENT_CMDS 8

struct recent_cmd {
	uint16_t cmd_id;
	uint8_t result;
	uint8_t reason;
	bool used;
};

struct pending_ev {
	uint8_t kind, reason;
	int16_t observed, threshold;
};

static struct k_spinlock lock;
static uint8_t state = B03_ST_BOOT;
static uint8_t fault_reason;
static bool fault_latched;
static bool synced;
static bool hb_valid;
static bool voice_active;
static bool mic_ok = true;
static bool soft_stopping;
static uint16_t setpoint_mms = 100;
static int64_t last_hb_ms;
static int64_t pe_blocked_since = -1;
static int64_t overcurrent_since = -1;
static bool estop_was_open;
static struct recent_cmd recent[RECENT_CMDS];
static uint8_t recent_next;
static struct eaa_sensors last_sens;

/* Events collected while holding the lock, sent after unlocking. */
static struct pending_ev evq[4];
static int evq_n;

#if DT_HAS_ALIAS(watchdog0) && (CONFIG_EAA_HW_WATCHDOG_MS > 0)
static const struct device *const wdt = DEVICE_DT_GET(DT_ALIAS(watchdog0));
static int wdt_ch = -1;
#endif

static void queue_event(uint8_t kind, uint8_t reason, int16_t obs, int16_t thr)
{
	if (evq_n < (int)ARRAY_SIZE(evq)) {
		evq[evq_n++] = (struct pending_ev){ kind, reason, obs, thr };
	}
}

static void flush_events(void)
{
	struct pending_ev local[ARRAY_SIZE(evq)];
	int n;
	k_spinlock_key_t key = k_spin_lock(&lock);

	n = evq_n;
	memcpy(local, evq, sizeof(local));
	evq_n = 0;
	k_spin_unlock(&lock, key);
	for (int i = 0; i < n; i++) {
		bridge_emit_event(local[i].kind, local[i].reason, local[i].observed,
				  local[i].threshold);
	}
}

/* Call with lock held. */
static void trip(uint8_t kind, uint8_t reason, int16_t obs, int16_t thr)
{
	bool first = !fault_latched;

	state = B03_ST_SAFE_STOP;
	fault_latched = true;
	if (first || fault_reason == B03_R_OK) {
		fault_reason = reason;
	}
	soft_stopping = false;
	queue_event(kind, reason, obs, thr);
}

static bool sensors_good(const struct eaa_sensors *s)
{
	if (!IS_ENABLED(CONFIG_EAA_REQUIRE_SENSORS)) {
		return true;
	}
	return s->current_ok && s->temp_ok;
}

/* Reason that blocks motion right now, or B03_R_OK. Lock held. */
static uint8_t motion_blocker(void)
{
	if (!synced) {
		return B03_R_NOT_SYNCED;
	}
	if (last_sens.estop_open) {
		return B03_R_ESTOP_ACTIVE;
	}
	if (fault_latched) {
		return B03_R_FAULT_LATCHED;
	}
	if (!hb_valid) {
		return B03_R_HEARTBEAT_TIMEOUT;
	}
	if (!sensors_good(&last_sens)) {
		return B03_R_SENSOR_LOSS;
	}
	return B03_R_OK;
}

/* Reason that prevents CLEAR_FAULT, or B03_R_OK. Lock held. */
static uint8_t clear_blocker(void)
{
	if (last_sens.estop_open) {
		return B03_R_ESTOP_ACTIVE;
	}
	if (!hb_valid) {
		return B03_R_HEARTBEAT_TIMEOUT;
	}
	if (last_sens.current_ok && last_sens.current_ma > CONFIG_EAA_MAX_CURRENT_MA) {
		return B03_R_MOTOR_OVERCURRENT;
	}
	if (last_sens.temp_ok && last_sens.temp_c10 != B03_TEMP_NOT_INSTALLED &&
	    last_sens.temp_c10 > CONFIG_EAA_MAX_TEMP_C10) {
		return B03_R_MOTOR_OVERTEMP;
	}
	if (!sensors_good(&last_sens)) {
		return B03_R_SENSOR_LOSS;
	}
	return B03_R_OK;
}

static uint8_t flags_locked(void)
{
	uint8_t f = 0;

	if (!last_sens.estop_open) {
		f |= B03_FLAG_ESTOP_CLOSED;
	}
	if (!last_sens.pe_blocked) {
		f |= B03_FLAG_PE_CLEAR;
	}
	if (hb_valid) {
		f |= B03_FLAG_HB_VALID;
	}
	if (fault_latched) {
		f |= B03_FLAG_FAULT_LATCHED;
	}
	if (state == B03_ST_RUNNING || soft_stopping) {
		f |= B03_FLAG_MOTOR_ENABLED;
	}
	if (last_sens.current_ok && last_sens.temp_ok && mic_ok) {
		f |= B03_FLAG_SENSORS_OK;
	}
	if (voice_active) {
		f |= B03_FLAG_VOICE_ACTIVE;
	}
	return f;
}

void safety_get_status(struct eaa_status *out)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	out->state = state;
	out->fault_reason = fault_reason;
	out->flags = flags_locked();
	out->setpoint_mms = setpoint_mms;
	k_spin_unlock(&lock, key);
}

void safety_target(bool *run, uint16_t *sp, bool *hard_stop)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	*run = (state == B03_ST_RUNNING);
	*sp = setpoint_mms;
	*hard_stop = (state == B03_ST_SAFE_STOP) || state == B03_ST_BOOT;
	/* soft stop finished? (control loop reports speed 0) */
	if (soft_stopping && control_speed_mms() == 0) {
		soft_stopping = false;
	}
	k_spin_unlock(&lock, key);
}

void safety_on_hello(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	synced = true;
	hb_valid = true;
	last_hb_ms = k_uptime_get();
	k_spin_unlock(&lock, key);
}

void safety_on_heartbeat(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	hb_valid = true;
	last_hb_ms = k_uptime_get();
	k_spin_unlock(&lock, key);
}

static struct recent_cmd *find_recent(uint16_t id)
{
	for (int i = 0; i < RECENT_CMDS; i++) {
		if (recent[i].used && recent[i].cmd_id == id) {
			return &recent[i];
		}
	}
	return NULL;
}

static void remember(uint16_t id, uint8_t result, uint8_t reason)
{
	recent[recent_next] = (struct recent_cmd){ id, result, reason, true };
	recent_next = (recent_next + 1) % RECENT_CMDS;
}

static uint8_t check_setpoint(uint16_t sp)
{
	if (sp > CONFIG_EAA_MAX_SPEED_MMS) {
		return B03_R_LIMIT_EXCEEDED;
	}
	if (sp < CONFIG_EAA_MIN_SPEED_MMS) {
		return B03_R_BAD_PARAM;
	}
	return B03_R_OK;
}

uint8_t safety_handle_cmd(const struct b03_cmd *c, uint8_t *reason)
{
	uint8_t res = B03_ACK_ACCEPTED;
	uint8_t why = B03_R_OK;
	k_spinlock_key_t key = k_spin_lock(&lock);
	struct recent_cmd *dup = find_recent(c->cmd_id);

	if (dup) {
		*reason = dup->reason;
		k_spin_unlock(&lock, key);
		return B03_ACK_DUPLICATE;
	}

	switch (c->type) {
	case B03_MT_RUN:
		why = motion_blocker();
		if (why == B03_R_OK && state != B03_ST_STOPPED && state != B03_ST_RUNNING) {
			why = B03_R_STATE_NOT_PERMITTED;
		}
		if (why == B03_R_OK) {
			why = check_setpoint(c->setpoint_mms);
		}
		if (why == B03_R_OK && c->ttl_ms == 0) {
			why = B03_R_EXPIRED;
		}
		if (why == B03_R_OK) {
			setpoint_mms = c->setpoint_mms;
			state = B03_ST_RUNNING;
			soft_stopping = false;
		}
		break;
	case B03_MT_SET_SPEED:
		if (fault_latched) {
			why = B03_R_FAULT_LATCHED;
		} else if (!synced) {
			why = B03_R_NOT_SYNCED;
		} else {
			why = check_setpoint(c->setpoint_mms);
		}
		if (why == B03_R_OK && c->ttl_ms == 0) {
			why = B03_R_EXPIRED;
		}
		if (why == B03_R_OK) {
			setpoint_mms = c->setpoint_mms;
		}
		break;
	case B03_MT_STOP: /* always permitted */
		if (state == B03_ST_RUNNING) {
			state = B03_ST_STOPPED;
			soft_stopping = true;
		}
		break;
	case B03_MT_HALT: /* always permitted, immediate, latched */
		trip(B03_EV_SAFE_STOP, B03_R_MPU_HALT_REQUEST, 0, 0);
		break;
	case B03_MT_CLEAR_FAULT:
		if (state == B03_ST_SAFE_STOP) {
			why = clear_blocker();
			if (why == B03_R_OK) {
				state = B03_ST_STOPPED;
				fault_latched = false;
				fault_reason = B03_R_OK;
				pe_blocked_since = -1;
				overcurrent_since = -1;
				queue_event(B03_EV_FAULT_CLEARED, B03_R_OK, 0, 0);
			}
		}
		break;
	default:
		why = B03_R_UNKNOWN_CMD;
		break;
	}
	if (why != B03_R_OK) {
		res = B03_ACK_REJECTED;
	}
	remember(c->cmd_id, res, why);
	k_spin_unlock(&lock, key);

	*reason = why;
	flush_events();
	return res;
}

void safety_voice_stop(void)
{
	/* Spoken "stop": controlled stop by the MCU itself, no MPU needed (CH-103). */
	k_spinlock_key_t key = k_spin_lock(&lock);

	if (state == B03_ST_RUNNING) {
		state = B03_ST_STOPPED;
		soft_stopping = true;
	}
	voice_active = false;
	k_spin_unlock(&lock, key);
}

void safety_set_voice_active(bool active)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	voice_active = active;
	k_spin_unlock(&lock, key);
}

void safety_set_mic_ok(bool ok)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	mic_ok = ok;
	k_spin_unlock(&lock, key);
}

static void check_invariants(const struct eaa_sensors *s, int64_t now)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	last_sens = *s;

	/* E-stop: any state */
	if (s->estop_open && !estop_was_open) {
		trip(B03_EV_HARDWARE_ESTOP, B03_R_ESTOP_ACTIVE, 0, 1);
	} else if (!s->estop_open && estop_was_open) {
		queue_event(B03_EV_ESTOP_RELEASED, B03_R_OK, 1, 1);
	}
	estop_was_open = s->estop_open;

	/* Heartbeat watchdog */
	if (synced && hb_valid && (now - last_hb_ms) > CONFIG_EAA_HB_TIMEOUT_MS) {
		hb_valid = false;
		if (state == B03_ST_RUNNING) {
			trip(B03_EV_HEARTBEAT_TIMEOUT, B03_R_HEARTBEAT_TIMEOUT,
			     (int16_t)MIN(now - last_hb_ms, INT16_MAX), CONFIG_EAA_HB_TIMEOUT_MS);
		}
	}

	/* Overcurrent with a short filter for the start-up spike */
	if (s->current_ok && s->current_ma > CONFIG_EAA_MAX_CURRENT_MA) {
		if (overcurrent_since < 0) {
			overcurrent_since = now;
		} else if ((now - overcurrent_since) >= CONFIG_EAA_OVERCURRENT_FILTER_MS &&
			   state != B03_ST_SAFE_STOP) {
			trip(B03_EV_MOTOR_OVERLOAD, B03_R_MOTOR_OVERCURRENT,
			     (int16_t)MIN(s->current_ma, INT16_MAX), CONFIG_EAA_MAX_CURRENT_MA);
		}
	} else {
		overcurrent_since = -1;
	}

	/* Overtemperature */
	if (s->temp_ok && s->temp_c10 != B03_TEMP_NOT_INSTALLED &&
	    s->temp_c10 > CONFIG_EAA_MAX_TEMP_C10 && state != B03_ST_SAFE_STOP) {
		trip(B03_EV_OVERTEMP, B03_R_MOTOR_OVERTEMP, s->temp_c10, CONFIG_EAA_MAX_TEMP_C10);
	}

	/* Jam: beam blocked for too long while running */
	if (s->pe_blocked && state == B03_ST_RUNNING) {
		if (pe_blocked_since < 0) {
			pe_blocked_since = now;
		} else if ((now - pe_blocked_since) >= CONFIG_EAA_JAM_TIMEOUT_MS) {
			trip(B03_EV_JAM_DETECTED, B03_R_PHOTOELECTRIC_BLOCKED,
			     (int16_t)MIN(now - pe_blocked_since, INT16_MAX),
			     CONFIG_EAA_JAM_TIMEOUT_MS);
		}
	} else {
		pe_blocked_since = -1;
	}

	/* Sensor loss while running */
	if (state == B03_ST_RUNNING && !sensors_good(s)) {
		trip(B03_EV_SENSOR_LOSS, B03_R_SENSOR_LOSS, s->status, 0);
	}
	k_spin_unlock(&lock, key);
}

static void safety_thread(void *a, void *b, void *c)
{
	struct eaa_sensors s;
	uint8_t last_state = 0xFF;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		int64_t now = k_uptime_get();

		sensors_get(&s);
		check_invariants(&s, now);
		flush_events();

#if DT_HAS_ALIAS(watchdog0) && (CONFIG_EAA_HW_WATCHDOG_MS > 0)
		if (wdt_ch >= 0) {
			wdt_feed(wdt, wdt_ch);
		}
#endif
		struct eaa_status st;

		safety_get_status(&st);
		if (st.state != last_state) {
			LOG_INF("state %u fault 0x%02x flags 0x%02x", st.state, st.fault_reason,
				st.flags);
			last_state = st.state;
		}
		k_sleep(K_MSEC(TICK_MS));
	}
}

K_THREAD_STACK_DEFINE(safety_stack, 1536);
static struct k_thread safety_tid;

bool safety_watchdog_active(void)
{
#if DT_HAS_ALIAS(watchdog0) && (CONFIG_EAA_HW_WATCHDOG_MS > 0)
	return wdt_ch >= 0;
#else
	return false;
#endif
}

void safety_watchdog_disable(void)
{
#if DT_HAS_ALIAS(watchdog0) && (CONFIG_EAA_HW_WATCHDOG_MS > 0)
	if (wdt_ch >= 0) {
		(void)wdt_disable(wdt);
		wdt_ch = -1;
	}
#endif
}

int safety_init(void)
{
#if DT_HAS_ALIAS(watchdog0) && (CONFIG_EAA_HW_WATCHDOG_MS > 0)
	if (device_is_ready(wdt)) {
		struct wdt_timeout_cfg cfg = {
			.window = { .min = 0, .max = CONFIG_EAA_HW_WATCHDOG_MS },
			.flags = WDT_FLAG_RESET_SOC,
		};

		wdt_ch = wdt_install_timeout(wdt, &cfg);
		if (wdt_ch >= 0 && wdt_setup(wdt, WDT_OPT_PAUSE_HALTED_BY_DBG) != 0) {
			wdt_ch = -1;
		}
		LOG_INF("hardware watchdog %s (%d ms)", wdt_ch >= 0 ? "on" : "FAILED",
			CONFIG_EAA_HW_WATCHDOG_MS);
	} else {
		LOG_WRN("hardware watchdog not ready");
	}
#endif
	/* Boot self-check: leave BOOT only once the first sensor snapshot exists. */
	sensors_get(&last_sens);
	estop_was_open = last_sens.estop_open;
	k_spinlock_key_t key = k_spin_lock(&lock);

	state = B03_ST_STOPPED;
	if (last_sens.estop_open) {
		state = B03_ST_SAFE_STOP;
		fault_latched = true;
		fault_reason = B03_R_ESTOP_ACTIVE;
	}
	k_spin_unlock(&lock, key);
	return 0;
}

void safety_start(void)
{
	k_thread_create(&safety_tid, safety_stack, K_THREAD_STACK_SIZEOF(safety_stack),
			safety_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(1), 0, K_NO_WAIT);
	k_thread_name_set(&safety_tid, "safety");
}
