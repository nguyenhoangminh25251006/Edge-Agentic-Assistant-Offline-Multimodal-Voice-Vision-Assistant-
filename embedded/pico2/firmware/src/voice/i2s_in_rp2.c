/* RP2350-only: I2S microphone capture with a PIO state machine (Zephyr has no I2S
 * driver for the RP2350). This is the only board-specific source file; on the
 * STM32U585 it is replaced by the Zephyr I2S/SAI driver behind voice/audio_in.h.
 *
 * PIO program (I2S master, 32-bit slots, 64 BCK per frame, sample on BCK rising edge):
 *   side-set bit0 = BCK (GPIO n), bit1 = WS/LRCK (GPIO n+1), IN pin = SD
 *       set x, 30      side 0b00   ; left slot starts, WS low
 *   l:  in pins, 1     side 0b01
 *       jmp x-- l      side 0b00
 *       in pins, 1     side 0b01   ; 32nd left bit, autopush
 *       set x, 30      side 0b10   ; right slot, WS high
 *   r:  in pins, 1     side 0b11
 *       jmp x-- r      side 0b10
 *       in pins, 1     side 0b11   ; 32nd right bit, autopush
 * Every instruction is one PIO clock, so BCK = PIO clock / 2 = 64 * fs.
 * The left word holds the I2S one-bit delay at bit 31 and the 24-bit sample in bits 30..7.
 * When the RX FIFO is full the state machine stalls with BCK stopped, so left/right
 * alignment is never lost (samples are dropped instead, counted as overruns).
 */
#include <zephyr/kernel.h>
#include <zephyr/irq.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/logging/log.h>
#include <hardware/pio.h>
#include <hardware/pio_instructions.h>
#include <hardware/regs/intctrl.h>
#include "audio_in.h"

LOG_MODULE_REGISTER(i2s_in, LOG_LEVEL_INF);

#define BCK_PIN CONFIG_EAA_I2S_PIN_BCK
#define SD_PIN  CONFIG_EAA_I2S_PIN_SD
#define SYS_HZ  DT_PROP(DT_NODELABEL(clk_sys), clock_frequency)
#define SS(v)   pio_encode_sideset(2, (v))

static uint16_t prog_instr[8];
static const pio_program_t prog = {
	.instructions = prog_instr,
	.length = 8,
	.origin = -1,
};

RING_BUF_DECLARE(mic_ring, 16000 * 2); /* 1 s of int16 */
static K_SEM_DEFINE(mic_sem, 0, 1);
static PIO pio = pio0;
static uint sm;
static bool odd;
static uint32_t overruns;
static uint32_t since_signal;

static void build_program(void)
{
	prog_instr[0] = pio_encode_set(pio_x, 30) | SS(0b00);
	prog_instr[1] = pio_encode_in(pio_pins, 1) | SS(0b01);
	prog_instr[2] = pio_encode_jmp_x_dec(1) | SS(0b00);
	prog_instr[3] = pio_encode_in(pio_pins, 1) | SS(0b01);
	prog_instr[4] = pio_encode_set(pio_x, 30) | SS(0b10);
	prog_instr[5] = pio_encode_in(pio_pins, 1) | SS(0b11);
	prog_instr[6] = pio_encode_jmp_x_dec(5) | SS(0b10);
	prog_instr[7] = pio_encode_in(pio_pins, 1) | SS(0b11);
}

static void mic_pio_isr(const void *arg)
{
	ARG_UNUSED(arg);

	while (!pio_sm_is_rx_fifo_empty(pio, sm)) {
		uint32_t w = pio_sm_get(pio, sm);

		if (!odd) {
			int32_t s24 = ((int32_t)(w << 1)) >> 8;
			int32_t s = s24 >> CONFIG_EAA_MIC_GAIN_SHIFT;
			int16_t v = (int16_t)CLAMP(s, INT16_MIN, INT16_MAX);

			if (ring_buf_put(&mic_ring, (uint8_t *)&v, 2) != 2) {
				overruns++;
			}
			if (++since_signal >= 256) {
				since_signal = 0;
				k_sem_give(&mic_sem);
			}
		}
		odd = !odd;
	}
}

int audio_in_init(uint32_t fs)
{
	build_program();
	int off = pio_add_program(pio, &prog);
	int s = pio_claim_unused_sm(pio, false);

	if (off < 0 || s < 0) {
		LOG_ERR("no free PIO program space / state machine");
		return -EBUSY;
	}
	sm = (uint)s;

	pio_gpio_init(pio, BCK_PIN);
	pio_gpio_init(pio, BCK_PIN + 1);
	pio_gpio_init(pio, SD_PIN);
	pio_sm_set_consecutive_pindirs(pio, sm, BCK_PIN, 2, true);
	pio_sm_set_consecutive_pindirs(pio, sm, SD_PIN, 1, false);

	pio_sm_config c = pio_get_default_sm_config();

	sm_config_set_wrap(&c, (uint)off, (uint)off + 7);
	sm_config_set_sideset(&c, 2, false, false);
	sm_config_set_sideset_pins(&c, BCK_PIN);
	sm_config_set_in_pins(&c, SD_PIN);
	sm_config_set_in_shift(&c, false, true, 32); /* MSB first, autopush 32 */
	sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
	sm_config_set_clkdiv(&c, (float)SYS_HZ / ((float)fs * 64.0f * 2.0f));
	pio_sm_init(pio, sm, (uint)off, &c);

	IRQ_CONNECT(PIO0_IRQ_0, 1, mic_pio_isr, NULL, 0);
	pio_set_irq0_source_enabled(pio, pio_get_rx_fifo_not_empty_interrupt_source(sm), true);
	irq_enable(PIO0_IRQ_0);
	pio_sm_set_enabled(pio, sm, true);
	LOG_INF("I2S mic: BCK GP%d, WS GP%d, SD GP%d, %u Hz, PIO0 SM%u", BCK_PIN, BCK_PIN + 1,
		SD_PIN, fs, sm);
	return 0;
}

size_t audio_in_read(int16_t *buf, size_t max, k_timeout_t timeout)
{
	uint32_t got = ring_buf_get(&mic_ring, (uint8_t *)buf, max * 2) / 2;

	if (got == 0) {
		(void)k_sem_take(&mic_sem, timeout);
		got = ring_buf_get(&mic_ring, (uint8_t *)buf, max * 2) / 2;
	}
	return got;
}

uint32_t audio_in_overruns(void)
{
	return overruns;
}
