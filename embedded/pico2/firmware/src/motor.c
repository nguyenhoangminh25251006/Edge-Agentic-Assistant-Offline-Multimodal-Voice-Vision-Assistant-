/* BTS7960 (IBT-2) driver: RPWM = forward PWM, LPWM held at 0 (belt runs one way),
 * R_EN + L_EN tied together on one GPIO with an external pull-down, so the driver
 * is disabled whenever the MCU is in reset or not driving the pin. */
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/logging/log.h>
#include "eaa.h"

LOG_MODULE_REGISTER(motor, LOG_LEVEL_INF);

#define USER DT_PATH(zephyr_user)

static const struct pwm_dt_spec rpwm = PWM_DT_SPEC_GET_BY_NAME(USER, rpwm);
static const struct pwm_dt_spec lpwm = PWM_DT_SPEC_GET_BY_NAME(USER, lpwm);
static const struct gpio_dt_spec en = GPIO_DT_SPEC_GET(USER, motor_en_gpios);

/* Hard cut path (T1): a safety trip or the E-stop interrupt calls motor_kill()
 * directly, so the motor stops in microseconds instead of waiting for the next
 * control tick. While killed, motor_apply() cannot re-enable the driver; the
 * kill is lifted only by CLEAR_FAULT (motor_unkill). The lock makes the
 * check-and-write in motor_apply() atomic against a kill from an ISR. */
static struct k_spinlock lock;
static bool killed;
static uint32_t t1_detect_cyc;  /* when the cause was detected */
static bool t1_loop_pending;
static struct eaa_t1_stats t1;

int motor_init(void)
{
	if (!pwm_is_ready_dt(&rpwm) || !pwm_is_ready_dt(&lpwm) || !gpio_is_ready_dt(&en)) {
		LOG_ERR("motor PWM/EN not ready");
		return -ENODEV;
	}
	int rc = gpio_pin_configure_dt(&en, GPIO_OUTPUT_INACTIVE);

	rc |= pwm_set_dt(&rpwm, CONFIG_EAA_PWM_PERIOD_NS, 0);
	rc |= pwm_set_dt(&lpwm, CONFIG_EAA_PWM_PERIOD_NS, 0);
	return rc;
}

static void write_locked(bool enable, uint16_t pwm_pm)
{
	uint32_t pulse = (uint32_t)(((uint64_t)CONFIG_EAA_PWM_PERIOD_NS * pwm_pm) / 1000U);

	if (!enable) { /* driver off first, then duty 0 */
		(void)gpio_pin_set_dt(&en, 0);
		(void)pwm_set_dt(&rpwm, CONFIG_EAA_PWM_PERIOD_NS, 0);
		return;
	}
	(void)pwm_set_dt(&rpwm, CONFIG_EAA_PWM_PERIOD_NS, pulse);
	(void)gpio_pin_set_dt(&en, 1);
}

void motor_apply(bool enable, uint16_t pwm_pm)
{
	if (pwm_pm > 1000) {
		pwm_pm = 1000;
	}
	k_spinlock_key_t key = k_spin_lock(&lock);

	if (killed) {
		enable = false;
	}
	write_locked(enable, enable ? pwm_pm : 0);
	k_spin_unlock(&lock, key);
}

void motor_kill(uint32_t detect_cyc)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	(void)gpio_pin_set_dt(&en, 0); /* BTS7960 R_EN/L_EN low: both half-bridges off */
	uint32_t us = k_cyc_to_us_ceil32(k_cycle_get_32() - detect_cyc);

	write_locked(false, 0);

	if (!killed) {
		killed = true;
		t1_detect_cyc = detect_cyc;
		t1_loop_pending = true;
		t1.count++;
		t1.last_us = us;
		t1.max_us = MAX(t1.max_us, us);
	}
	k_spin_unlock(&lock, key);
}

void motor_unkill(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	killed = false;
	k_spin_unlock(&lock, key);
}

void motor_loop_saw_stop(void)
{
	/* Reference only: how long the old path (wait for the control tick) would
	 * have taken for the same event. */
	k_spinlock_key_t key = k_spin_lock(&lock);

	if (t1_loop_pending) {
		uint32_t us = k_cyc_to_us_ceil32(k_cycle_get_32() - t1_detect_cyc);

		t1_loop_pending = false;
		t1.loop_last_us = us;
		t1.loop_max_us = MAX(t1.loop_max_us, us);
	}
	k_spin_unlock(&lock, key);
}

void motor_get_t1(struct eaa_t1_stats *out, bool reset)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	*out = t1;
	if (reset) {
		memset(&t1, 0, sizeof(t1));
	}
	k_spin_unlock(&lock, key);
}
