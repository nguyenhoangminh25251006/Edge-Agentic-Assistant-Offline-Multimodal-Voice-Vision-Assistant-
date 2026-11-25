/* BTS7960 (IBT-2) driver: RPWM = forward PWM, LPWM held at 0 (belt runs one way),
 * R_EN + L_EN tied together on one GPIO with an external pull-down, so the driver
 * is disabled whenever the MCU is in reset or not driving the pin. */
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

void motor_apply(bool enable, uint16_t pwm_pm)
{
	if (pwm_pm > 1000) {
		pwm_pm = 1000;
	}
	if (!enable) {
		pwm_pm = 0;
	}
	uint32_t pulse = (uint32_t)(((uint64_t)CONFIG_EAA_PWM_PERIOD_NS * pwm_pm) / 1000U);

	(void)pwm_set_dt(&rpwm, CONFIG_EAA_PWM_PERIOD_NS, pulse);
	(void)gpio_pin_set_dt(&en, enable ? 1 : 0);
}
