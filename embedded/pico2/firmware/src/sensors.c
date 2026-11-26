/* Sensors: E-stop auxiliary contact, photoelectric E18-D80NK, INA226 (current),
 * MLX90614 (motor temperature). Raw Zephyr GPIO / I2C APIs only, so the same
 * file runs on the STM32U585 with a different devicetree.
 *
 * I2C is slow and can stall, so it runs here at 20 ms and never inside the
 * control loop. The E-stop pin raises an interrupt that cuts the motor at once
 * (T1), and the control loop also reads the level every tick (sensors_estop_open_now).
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/logging/log.h>
#include "eaa.h"

LOG_MODULE_REGISTER(sensors, LOG_LEVEL_INF);

#define USER DT_PATH(zephyr_user)
#define PERIOD_MS 20
#define MLX_EVERY 5   /* MLX90614 every 100 ms */
#define FAIL_LIMIT 3  /* consecutive I2C errors before a sensor counts as lost */

static const struct gpio_dt_spec estop = GPIO_DT_SPEC_GET(USER, estop_gpios);
static const struct gpio_dt_spec pe = GPIO_DT_SPEC_GET(USER, pe_gpios);
static struct gpio_callback estop_cb;

/* E-stop contact opens -> cut the motor from the interrupt itself (T1 path).
 * No debounce: a glitch only causes a latched safe stop, never a missed one.
 * The 20 ms poll below still reads the level for the FSM and telemetry. */
static void on_estop_edge(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);
	safety_estop_isr(k_cycle_get_32());
}

#if DT_HAS_CHOSEN(eaa_sensor_i2c)
static const struct device *const i2c = DEVICE_DT_GET(DT_CHOSEN(eaa_sensor_i2c));
#else
static const struct device *const i2c;
#endif

static struct k_spinlock lock;
static struct eaa_sensors snap = {
	.estop_open = true, /* safe default until the first read */
	.current_ok = !IS_ENABLED(CONFIG_EAA_INA226),
	.temp_ok = !IS_ENABLED(CONFIG_EAA_MLX90614),
	.temp_c10 = B03_TEMP_NOT_INSTALLED,
};
static int ina_fail __maybe_unused;
static int mlx_fail __maybe_unused;

bool sensors_estop_open_now(void)
{
	/* Pin is active (logical 1) when the NC contact is open. Read errors count as open. */
	return gpio_pin_get_dt(&estop) != 0;
}

#if defined(CONFIG_SOC_SERIES_RP2350)
/* Bench test of the T1 path without touching the wiring: the RP2350 IO bank can
 * override a pad's input (GPIOx_CTRL.INOVER), so the E-stop pin really reads
 * "open" and the edge interrupt fires through the normal GPIO path. */
#define IO_BANK0_CTRL(n) ((volatile uint32_t *)(0x40028000u + 8u * (n) + 4u))
#define INOVER_SHIFT 16
int sensors_estop_inject(bool open)
{
	bool active_low = (estop.dt_flags & GPIO_ACTIVE_LOW) != 0;
	uint32_t ov = !open ? 0u : (active_low ? 2u : 3u); /* 2 = force low, 3 = force high */
	volatile uint32_t *ctrl = IO_BANK0_CTRL(estop.pin);

	*ctrl = (*ctrl & ~(3u << INOVER_SHIFT)) | (ov << INOVER_SHIFT);
	return 0;
}
#elif defined(CONFIG_SOC_SERIES_STM32U5X)
/* STM32U585 (UNO Q): no pad override, but the EXTI software-trigger register
 * raises the same EXTI line, so the same ISR and T1 path run. The pin level
 * itself still reads "closed"; the trip is latched by the ISR. */
#include <soc.h>
int sensors_estop_inject(bool open)
{
	if (open) {
		EXTI->SWIER1 = BIT(estop.pin);
	}
	return 0;
}
#else
int sensors_estop_inject(bool open)
{
	ARG_UNUSED(open);
	return -ENOTSUP;
}
#endif

void sensors_get(struct eaa_sensors *out)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	*out = snap;
	k_spin_unlock(&lock, key);
}

#if IS_ENABLED(CONFIG_EAA_INA226)
#define INA_REG_CONFIG  0x00
#define INA_REG_BUS     0x02
#define INA_REG_CURRENT 0x04
#define INA_REG_CAL     0x05
/* Current LSB = 1 mA -> CAL = 0.00512 / (0.001 A * R) = 5120 / R[mOhm] */
#define INA_CAL (5120 / CONFIG_EAA_INA226_SHUNT_MOHM)

static int ina_write(uint8_t reg, uint16_t v)
{
	uint8_t b[3] = { reg, (uint8_t)(v >> 8), (uint8_t)v };

	return i2c_write(i2c, b, sizeof(b), CONFIG_EAA_INA226_ADDR);
}

static int ina_read(uint8_t reg, uint16_t *v)
{
	uint8_t b[2];
	int rc = i2c_write_read(i2c, CONFIG_EAA_INA226_ADDR, &reg, 1, b, 2);

	*v = (uint16_t)((b[0] << 8) | b[1]);
	return rc;
}

static int ina_setup(void)
{
	/* AVG=16, VBUSCT=1.1 ms, VSHCT=1.1 ms, continuous shunt+bus */
	int rc = ina_write(INA_REG_CONFIG, 0x4527);

	return rc ? rc : ina_write(INA_REG_CAL, INA_CAL);
}
#endif

#if IS_ENABLED(CONFIG_EAA_MLX90614)
static uint8_t crc8_smbus(const uint8_t *d, size_t n)
{
	uint8_t c = 0;

	for (size_t i = 0; i < n; i++) {
		c ^= d[i];
		for (int b = 0; b < 8; b++) {
			c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
		}
	}
	return c;
}

/* Object temperature Tobj1 (RAM 0x07), 0.02 K per LSB, with PEC check. */
static int mlx_read_c10(int16_t *c10)
{
	uint8_t reg = 0x07;
	uint8_t b[3];
	int rc = i2c_write_read(i2c, CONFIG_EAA_MLX90614_ADDR, &reg, 1, b, 3);

	if (rc) {
		return rc;
	}
	uint8_t pec_in[5] = { CONFIG_EAA_MLX90614_ADDR << 1, reg,
			      (CONFIG_EAA_MLX90614_ADDR << 1) | 1, b[0], b[1] };

	if (crc8_smbus(pec_in, sizeof(pec_in)) != b[2]) {
		return -EIO;
	}
	uint16_t raw = (uint16_t)(b[0] | (b[1] << 8));

	if (raw & 0x8000) { /* error flag */
		return -EIO;
	}
	/* T[0.1 C] = raw * 0.2 - 2731.5 */
	*c10 = (int16_t)(((int32_t)raw * 2 - 27315 + 5) / 10);
	return 0;
}
#endif

static void sensors_thread(void *a, void *b, void *c)
{
	uint32_t n = 0;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		struct eaa_sensors s;

		sensors_get(&s);
		s.estop_open = sensors_estop_open_now();
		s.pe_blocked = gpio_pin_get_dt(&pe) == 1;

#if IS_ENABLED(CONFIG_EAA_INA226)
		uint16_t cur, bus;

		if (ina_read(INA_REG_CURRENT, &cur) == 0 && ina_read(INA_REG_BUS, &bus) == 0) {
			int16_t ma = (int16_t)cur;

			s.current_ma = (uint16_t)(ma < 0 ? -ma : ma);
			s.bus_mv = (uint16_t)((bus * 5U) / 4U); /* 1.25 mV per LSB */
			ina_fail = 0;
			s.current_ok = true;
		} else if (++ina_fail >= FAIL_LIMIT) {
			s.current_ok = false;
			if (ina_fail == FAIL_LIMIT) {
				LOG_WRN("INA226 lost");
			}
			if (ina_fail % 50 == 0) {
				(void)ina_setup(); /* sensor re-plugged? */
			}
		}
#endif
#if IS_ENABLED(CONFIG_EAA_MLX90614)
		if ((n % MLX_EVERY) == 0) {
			int16_t t;

			if (mlx_read_c10(&t) == 0) {
				s.temp_c10 = t;
				mlx_fail = 0;
				s.temp_ok = true;
			} else if (++mlx_fail >= FAIL_LIMIT) {
				s.temp_ok = false;
				s.temp_c10 = B03_TEMP_FAULT;
				if (mlx_fail == FAIL_LIMIT) {
					LOG_WRN("MLX90614 lost");
				}
			}
		}
#endif
		s.status = 0;
		if (!s.current_ok) {
			s.status |= EAA_SS_CURRENT_FAULT;
		}
		if (!s.temp_ok) {
			s.status |= EAA_SS_TEMP_FAULT;
		}
		if (!encoder_fitted()) {
			s.status |= EAA_SS_ENCODER_ABSENT;
		}
		if (s.pe_blocked) {
			s.status |= EAA_SS_PE_BLOCKED;
		}

		k_spinlock_key_t key = k_spin_lock(&lock);

		snap = s;
		k_spin_unlock(&lock, key);
		n++;
		k_sleep(K_MSEC(PERIOD_MS));
	}
}

K_THREAD_STACK_DEFINE(sensors_stack, 1536);
static struct k_thread sensors_tid;

int sensors_init(void)
{
	int rc = 0;

	if (!gpio_is_ready_dt(&estop) || !gpio_is_ready_dt(&pe)) {
		return -ENODEV;
	}
	rc |= gpio_pin_configure_dt(&estop, GPIO_INPUT);
	rc |= gpio_pin_configure_dt(&pe, GPIO_INPUT);
	gpio_init_callback(&estop_cb, on_estop_edge, BIT(estop.pin));
	rc |= gpio_add_callback_dt(&estop, &estop_cb);
	rc |= gpio_pin_interrupt_configure_dt(&estop, GPIO_INT_EDGE_TO_ACTIVE);

	if (IS_ENABLED(CONFIG_EAA_INA226) || IS_ENABLED(CONFIG_EAA_MLX90614)) {
		if (i2c == NULL || !device_is_ready(i2c)) {
			LOG_ERR("sensor I2C bus not ready");
			return -ENODEV;
		}
	}
#if IS_ENABLED(CONFIG_EAA_INA226)
	if (ina_setup() != 0) {
		LOG_WRN("INA226 not answering at 0x%02x", CONFIG_EAA_INA226_ADDR);
	}
#endif
	/* First snapshot before safety leaves BOOT */
	k_spinlock_key_t key = k_spin_lock(&lock);

	snap.estop_open = sensors_estop_open_now();
	snap.pe_blocked = gpio_pin_get_dt(&pe) == 1;
	k_spin_unlock(&lock, key);
	return rc;
}

void sensors_start(void)
{
	k_thread_create(&sensors_tid, sensors_stack, K_THREAD_STACK_SIZEOF(sensors_stack),
			sensors_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(4), 0, K_NO_WAIT);
	k_thread_name_set(&sensors_tid, "sensors");
}
