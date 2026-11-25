/* Command-audio channel to the MPU on the chosen "eaa,audio-uart".
 * Pico 2: a second USB CDC ACM port (the Pi sees /dev/ttyACM1). UNO Q: a second
 * UART at >= 921600 baud or SPI (CH-103) - same Zephyr UART API, only the
 * devicetree changes. Interrupt-driven TX from a ring buffer, so the voice thread
 * never blocks; when the MPU is not reading, frames are dropped and counted.
 *
 * Audio frame (little-endian):
 *   0xA5 0x5A | type u8 | seq u16 | len u16 | payload[len] | crc16 CCITT-FALSE(type..payload)
 *   type 1 START payload: event_seq u16, sample_rate u16, bits u8 (16), channels u8 (1)
 *   type 2 DATA  payload: int16 samples
 *   type 3 END   payload: total_samples u32
 * Reader: embedded/pi/audio_rx.py.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/logging/log.h>
#include "proto/crc16.h"
#include "audio_out.h"

LOG_MODULE_REGISTER(audio_out, LOG_LEVEL_INF);

#if DT_HAS_CHOSEN(eaa_audio_uart)
static const struct device *const dev = DEVICE_DT_GET(DT_CHOSEN(eaa_audio_uart));
#else
static const struct device *const dev;
#endif

RING_BUF_DECLARE(tx_ring, 8192);
static uint16_t seq;
static uint32_t dropped;
static bool ready;

static void isr(const struct device *d, void *user)
{
	ARG_UNUSED(user);

	while (uart_irq_update(d) && uart_irq_is_pending(d)) {
		if (uart_irq_tx_ready(d)) {
			uint8_t *p;
			uint32_t n = ring_buf_get_claim(&tx_ring, &p, 64);

			if (n == 0) {
				uart_irq_tx_disable(d);
				break;
			}
			int w = uart_fifo_fill(d, p, (int)n);

			ring_buf_get_finish(&tx_ring, w > 0 ? (uint32_t)w : 0);
		} else {
			break;
		}
	}
}

int audio_out_init(void)
{
	if (dev == NULL || !device_is_ready(dev)) {
		LOG_WRN("no audio channel (chosen eaa,audio-uart)");
		return -ENODEV;
	}
	uart_irq_callback_user_data_set(dev, isr, NULL);
	ready = true;
	return 0;
}

static void send(uint8_t type, const uint8_t *payload, uint16_t len)
{
	uint8_t hdr[7] = { 0xA5, 0x5A, type, (uint8_t)seq, (uint8_t)(seq >> 8), (uint8_t)len,
			   (uint8_t)(len >> 8) };
	uint16_t crc = crc16_ccitt_false(&hdr[2], 5, CRC16_INIT);

	crc = crc16_ccitt_false(payload, len, crc);
	uint8_t tail[2] = { (uint8_t)crc, (uint8_t)(crc >> 8) };

	if (!ready) {
		return;
	}
	seq++;
	unsigned int key = irq_lock();

	if (ring_buf_space_get(&tx_ring) < (uint32_t)(sizeof(hdr) + len + sizeof(tail))) {
		irq_unlock(key);
		dropped++;
		return;
	}
	ring_buf_put(&tx_ring, hdr, sizeof(hdr));
	ring_buf_put(&tx_ring, payload, len);
	ring_buf_put(&tx_ring, tail, sizeof(tail));
	irq_unlock(key);
	uart_irq_tx_enable(dev);
}

void audio_out_begin(uint16_t event_seq, uint16_t sample_rate)
{
	uint8_t p[6] = { (uint8_t)event_seq, (uint8_t)(event_seq >> 8), (uint8_t)sample_rate,
			 (uint8_t)(sample_rate >> 8), 16, 1 };

	send(1, p, sizeof(p));
}

void audio_out_samples(const int16_t *s, size_t n)
{
	send(2, (const uint8_t *)s, (uint16_t)(n * 2));
}

void audio_out_end(uint32_t total)
{
	uint8_t p[4] = { (uint8_t)total, (uint8_t)(total >> 8), (uint8_t)(total >> 16),
			 (uint8_t)(total >> 24) };

	send(3, p, sizeof(p));
}

uint32_t audio_out_dropped(void)
{
	return dropped;
}
