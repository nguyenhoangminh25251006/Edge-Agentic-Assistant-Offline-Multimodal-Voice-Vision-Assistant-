/* Belt control loop, CONFIG_EAA_CONTROL_PERIOD_US (default 10 ms = 100 Hz; the
 * derivation is in REPORT.md "Control loop rate"). Highest-priority cooperative
 * thread, woken by a k_timer.
 *
 *  - hard stop: safety trips and the E-stop interrupt already cut the driver
 *    directly (motor_kill, T1 in microseconds); this loop keeps it off
 *    (SAFE_STOP / E-stop pin open)
 *  - controlled stop (STOP, spoken "stop"): ramp the setpoint down, then disable
 *  - open loop (no encoder, CH-107): feed-forward pwm = offset + gain * setpoint
 *  - closed loop (encoder): feed-forward + PI on measured mm/s
 * It also measures its own period jitter for NFR-01 (<= 1 ms = 10 % of the period).
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/logging/log.h>
#include "eaa.h"

LOG_MODULE_REGISTER(control, LOG_LEVEL_INF);

#define PERIOD_US CONFIG_EAA_CONTROL_PERIOD_US

static K_SEM_DEFINE(tick_sem, 0, 1);
static struct k_timer tick_timer;
static atomic_t speed_mms;
static atomic_t pwm_pm;
static struct k_spinlock stat_lock;
static struct eaa_control_stats stats;

uint16_t control_speed_mms(void)
{
	return (uint16_t)atomic_get(&speed_mms);
}

uint16_t control_pwm_pm(void)
{
	return (uint16_t)atomic_get(&pwm_pm);
}

void control_get_stats(struct eaa_control_stats *out, bool reset)
{
	k_spinlock_key_t key = k_spin_lock(&stat_lock);

	*out = stats;
	if (reset) {
		stats.jitter_max_us = 0;
		stats.exec_max_us = 0;
	}
	k_spin_unlock(&stat_lock, key);
}

static void on_tick(struct k_timer *t)
{
	ARG_UNUSED(t);
	k_sem_give(&tick_sem);
}

static int32_t feed_forward(int32_t sp)
{
	if (sp <= 0) {
		return 0;
	}
	return CONFIG_EAA_OPENLOOP_PWM_OFFSET_PM + (sp * CONFIG_EAA_OPENLOOP_PWM_GAIN_X100) / 100;
}

static void control_thread(void *a, void *b, void *c)
{
	const int32_t ramp_per_tick_x1000 =
		(CONFIG_EAA_RAMP_MMS_PER_S * PERIOD_US) / 1000; /* mm/s * 1000 per tick */
	int32_t cmd_x1000 = 0;  /* ramped setpoint, mm/s * 1000 */
	int64_t integ = 0;      /* PI integrator, per mille * 1e6 */
	uint32_t last_cyc = 0;
	bool first = true;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		k_sem_take(&tick_sem, K_FOREVER);
		uint32_t t0 = k_cycle_get_32();

		/* --- jitter measurement --- */
		if (!first) {
			uint32_t dt_us = k_cyc_to_us_floor32(t0 - last_cyc);
			uint32_t j = dt_us > PERIOD_US ? dt_us - PERIOD_US : PERIOD_US - dt_us;
			k_spinlock_key_t key = k_spin_lock(&stat_lock);

			if (j > stats.jitter_max_us) {
				stats.jitter_max_us = j;
			}
			stats.loops++;
			k_spin_unlock(&stat_lock, key);
		}
		first = false;
		last_cyc = t0;

		/* --- decide --- */
		bool run, hard;
		uint16_t sp;

		safety_target(&run, &sp, &hard);
		if (sensors_estop_open_now()) {
			hard = true; /* do not wait for the 10 ms safety tick */
		}

		int32_t target_x1000 = (run && !hard) ? (int32_t)sp * 1000 : 0;

		if (hard) {
			motor_loop_saw_stop(); /* reference: old tick-based T1 */
			cmd_x1000 = 0;
			integ = 0;
		} else if (cmd_x1000 < target_x1000) {
			cmd_x1000 = MIN(cmd_x1000 + ramp_per_tick_x1000, target_x1000);
		} else if (cmd_x1000 > target_x1000) {
			cmd_x1000 = MAX(cmd_x1000 - ramp_per_tick_x1000, target_x1000);
		}
		int32_t cmd = cmd_x1000 / 1000;

		/* --- measure --- */
		int32_t meas = cmd; /* open loop: estimate = ramped setpoint */
#if IS_ENABLED(CONFIG_EAA_ENCODER)
		int32_t counts = encoder_take_counts();

		/* mm/s = counts / CPR * circ_um / 1000 / (period_us / 1e6) */
		meas = (int32_t)(((int64_t)counts * CONFIG_EAA_ROLLER_CIRC_UM * 1000) /
				 ((int64_t)CONFIG_EAA_ENC_COUNTS_PER_REV * PERIOD_US));
		if (meas < 0) {
			meas = -meas;
		}
#endif
		/* --- actuate --- */
		int32_t pwm = feed_forward(cmd);
#if IS_ENABLED(CONFIG_EAA_ENCODER)
		if (cmd > 0) {
			int32_t err = cmd - meas;

			integ += (int64_t)err * CONFIG_EAA_SPEED_KI_X1000 * PERIOD_US / 1000;
			integ = CLAMP(integ, -300000000LL, 300000000LL); /* +-300 per mille */
			pwm += (err * CONFIG_EAA_SPEED_KP_X1000) / 1000 + (int32_t)(integ / 1000000);
		} else {
			integ = 0;
		}
#endif
		pwm = CLAMP(pwm, 0, CONFIG_EAA_MAX_PWM_PM);
		bool enable = !hard && (cmd > 0);

		if (!enable) {
			pwm = 0;
		}
		motor_apply(enable, (uint16_t)pwm);
		atomic_set(&pwm_pm, pwm);
		atomic_set(&speed_mms, enable ? meas : 0);

		uint32_t exec_us = k_cyc_to_us_floor32(k_cycle_get_32() - t0);
		k_spinlock_key_t key = k_spin_lock(&stat_lock);

		if (exec_us > stats.exec_max_us) {
			stats.exec_max_us = exec_us;
		}
		k_spin_unlock(&stat_lock, key);
	}
}

K_THREAD_STACK_DEFINE(control_stack, 1024);
static struct k_thread control_tid;

void control_start(void)
{
	k_thread_create(&control_tid, control_stack, K_THREAD_STACK_SIZEOF(control_stack),
			control_thread, NULL, NULL, NULL, K_PRIO_COOP(1), 0, K_NO_WAIT);
	k_thread_name_set(&control_tid, "control");
	k_timer_init(&tick_timer, on_tick, NULL);
	k_timer_start(&tick_timer, K_USEC(PERIOD_US), K_USEC(PERIOD_US));
	LOG_INF("control loop %d us, %s", PERIOD_US,
		IS_ENABLED(CONFIG_EAA_ENCODER) ? "closed loop (encoder)" : "open loop");
}
