/* Quadrature encoder, x4 decoding with GPIO edge interrupts (portable Zephyr GPIO API).
 * Enough for belt speeds (a few kHz of edges). On the STM32U585 the hardware
 * timer encoder mode (Zephyr qdec sensor) can replace this file later.
 * CH-107: the encoder is not fitted yet -> CONFIG_EAA_ENCODER=n by default. */
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/atomic.h>
#include "eaa.h"

#if IS_ENABLED(CONFIG_EAA_ENCODER)
#define USER DT_PATH(zephyr_user)

static const struct gpio_dt_spec a = GPIO_DT_SPEC_GET(USER, enc_a_gpios);
static const struct gpio_dt_spec b = GPIO_DT_SPEC_GET(USER, enc_b_gpios);
static struct gpio_callback cb_a, cb_b;
static atomic_t count;
static uint8_t prev;

/* index = (prev << 2) | now, value = step */
static const int8_t qdec[16] = { 0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0 };

static void on_edge(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);
	uint8_t now = (uint8_t)((gpio_pin_get_dt(&a) << 1) | gpio_pin_get_dt(&b));

	atomic_add(&count, qdec[(prev << 2) | now]);
	prev = now;
}

int encoder_init(void)
{
	if (!gpio_is_ready_dt(&a) || !gpio_is_ready_dt(&b)) {
		return -ENODEV;
	}
	gpio_pin_configure_dt(&a, GPIO_INPUT);
	gpio_pin_configure_dt(&b, GPIO_INPUT);
	prev = (uint8_t)((gpio_pin_get_dt(&a) << 1) | gpio_pin_get_dt(&b));
	gpio_init_callback(&cb_a, on_edge, BIT(a.pin));
	gpio_init_callback(&cb_b, on_edge, BIT(b.pin));
	gpio_add_callback_dt(&a, &cb_a);
	gpio_add_callback_dt(&b, &cb_b);
	gpio_pin_interrupt_configure_dt(&a, GPIO_INT_EDGE_BOTH);
	gpio_pin_interrupt_configure_dt(&b, GPIO_INT_EDGE_BOTH);
	return 0;
}

bool encoder_fitted(void)
{
	return true;
}

int32_t encoder_take_counts(void)
{
	return (int32_t)atomic_set(&count, 0);
}
#else
int encoder_init(void)
{
	return 0;
}

bool encoder_fitted(void)
{
	return false;
}

int32_t encoder_take_counts(void)
{
	return 0;
}
#endif
