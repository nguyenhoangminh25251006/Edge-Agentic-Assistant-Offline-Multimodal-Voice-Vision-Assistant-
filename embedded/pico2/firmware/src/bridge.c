/* B-03 v1.1 bridge on the chosen "eaa,bridge-uart" (Pico 2: UART0 GP0/GP1 to the Pi 3B).
 *
 * Wire: COBS-encoded frame + 0x00 delimiter (PipeTransport, Profile B) at 115200 8N1.
 * RX: UART interrupt -> ring buffer -> this thread splits on 0x00, decodes, dispatches.
 * TX: only this thread writes to the UART (uart_poll_out), so frames never interleave.
 * Behaviour follows orchestrator:src/control_pipeline/bridge/mock_mcu.py:
 *   HELLO -> HELLO_ACK, HEARTBEAT -> HB_VALID, commands -> CMD_ACK, TELEMETRY every 50 ms,
 *   MCU_EVENT / VOICE_EVENT retransmitted until EVENT_ACK (each retransmit gets a new
 *   frame seq; the event_seq stays the same so the MPU can de-duplicate).
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/logging/log.h>
#include "eaa.h"
#include "proto/cobs.h"

LOG_MODULE_REGISTER(bridge, LOG_LEVEL_INF);

#define MAX_PENDING 8
#define RX_ENC_MAX  (COBS_MAX_ENCODED(B03_MAX_FRAME) + 4)

static const struct device *const uart = DEVICE_DT_GET(DT_CHOSEN(eaa_bridge_uart));

RING_BUF_DECLARE(rx_ring, 256);
static K_SEM_DEFINE(rx_sem, 0, 1);

struct ev_req {
	bool voice;
	uint8_t kind, reason_or_conf;
	int16_t observed, threshold;
	uint16_t model_ver;
};
K_MSGQ_DEFINE(ev_q, sizeof(struct ev_req), 16, 4);

struct pending {
	bool used;
	uint16_t event_seq;
	struct ev_req req;
	uint8_t mcu_state; /* state when the event happened */
	int64_t last_tx_ms;
	uint8_t tries;
};

static struct pending pend[MAX_PENDING];
static uint16_t tx_seq;
static uint16_t last_rx_seq;
static uint16_t event_seq;
static uint16_t boot_id;
static struct eaa_bridge_stats st;
static struct k_spinlock st_lock;

void bridge_get_stats(struct eaa_bridge_stats *out)
{
	k_spinlock_key_t key = k_spin_lock(&st_lock);

	*out = st;
	out->events_pending = 0;
	for (int i = 0; i < MAX_PENDING; i++) {
		out->events_pending += pend[i].used ? 1 : 0;
	}
	k_spin_unlock(&st_lock, key);
}

void bridge_emit_event(uint8_t kind, uint8_t reason, int16_t observed, int16_t threshold)
{
	struct ev_req r = { .voice = false, .kind = kind, .reason_or_conf = reason,
			    .observed = observed, .threshold = threshold };

	if (k_msgq_put(&ev_q, &r, K_NO_WAIT) != 0) {
		LOG_ERR("event queue full, kind 0x%02x lost", kind);
	}
}

void bridge_emit_voice(uint8_t kind, uint8_t confidence_pct, uint16_t model_ver)
{
	struct ev_req r = { .voice = true, .kind = kind, .reason_or_conf = confidence_pct,
			    .model_ver = model_ver };

	if (k_msgq_put(&ev_q, &r, K_NO_WAIT) != 0) {
		LOG_ERR("event queue full, voice kind %u lost", kind);
	}
}

static void uart_isr(const struct device *dev, void *user)
{
	ARG_UNUSED(user);
	uint8_t buf[32];

	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			int n = uart_fifo_read(dev, buf, sizeof(buf));

			if (n > 0) {
				ring_buf_put(&rx_ring, buf, n);
				k_sem_give(&rx_sem);
			}
		}
	}
}

static void send_frame(struct b03_frame *f)
{
	uint8_t raw[B03_MAX_FRAME];
	uint8_t enc[COBS_MAX_ENCODED(B03_MAX_FRAME) + 1];
	struct eaa_status s;

	safety_get_status(&s);
	f->seq = tx_seq++;
	f->ack = last_rx_seq;
	f->tick_ms = k_uptime_get_32();
	f->flags = s.flags;
	int n = b03_encode(f, raw, sizeof(raw));

	if (n < 0) {
		return;
	}
	size_t m = cobs_encode(raw, (size_t)n, enc);

	enc[m++] = 0x00;
	for (size_t i = 0; i < m; i++) {
		uart_poll_out(uart, enc[i]);
	}
	st.tx_frames++;
}

static void send_event(struct pending *p)
{
	struct b03_frame f;

	if (p->req.voice) {
		b03_pl_voice_event(&f, p->req.kind, p->req.reason_or_conf, p->event_seq,
				   p->mcu_state, p->req.model_ver);
	} else {
		b03_pl_mcu_event(&f, p->req.kind, p->req.reason_or_conf, p->event_seq,
				 p->mcu_state, p->req.observed, p->req.threshold);
	}
	send_frame(&f);
	p->last_tx_ms = k_uptime_get();
	p->tries++;
}

static void send_telemetry(void)
{
	struct b03_frame f;
	struct eaa_status s;
	struct eaa_sensors sen;

	safety_get_status(&s);
	sensors_get(&sen);
	b03_pl_telemetry(&f, s.state, s.fault_reason, s.setpoint_mms, control_speed_mms(),
			 sen.current_ma, sen.temp_c10, sen.status, control_pwm_pm());
	send_frame(&f);
}

static void handle_frame(const struct b03_frame *f)
{
	struct b03_cmd c;
	struct b03_frame out;
	struct eaa_status s;
	int rc = b03_parse(f, &c);

	last_rx_seq = f->seq;
	if (rc == B03_R_UNKNOWN_CMD) {
		LOG_WRN("unknown msg type 0x%02x", f->type);
		return;
	}
	if (rc != 0) {
		LOG_WRN("short payload, type 0x%02x len %u", f->type, f->len);
		return;
	}

	switch (c.type) {
	case B03_MT_HELLO:
		safety_on_hello();
		safety_get_status(&s);
		b03_pl_hello_ack(&out, boot_id, CONFIG_EAA_FW_VERSION, s.state, s.fault_reason);
		send_frame(&out);
		if (!st.synced) {
			LOG_INF("HELLO from MPU, session 0x%08x, proto %u", c.session_id,
				c.proto_ver);
		}
		st.synced = true;
		break;
	case B03_MT_HEARTBEAT:
		safety_on_heartbeat();
		break;
	case B03_MT_EVENT_ACK:
		for (int i = 0; i < MAX_PENDING; i++) {
			if (pend[i].used && pend[i].event_seq == c.event_seq) {
				pend[i].used = false;
			}
		}
		break;
	default: {
		uint8_t reason;
		uint8_t result = safety_handle_cmd(&c, &reason);

		safety_get_status(&s);
		b03_pl_cmd_ack(&out, c.cmd_id, result, reason, s.state);
		send_frame(&out);
		LOG_INF("cmd 0x%02x id %u -> result %u reason 0x%02x state %u", c.type, c.cmd_id,
			result, reason, s.state);
		break;
	}
	}
}

static void process_rx(void)
{
	static uint8_t enc[RX_ENC_MAX];
	static size_t enc_len;
	static bool overflow;
	uint8_t byte;

	while (ring_buf_get(&rx_ring, &byte, 1) == 1) {
		if (byte != 0x00) {
			if (enc_len < sizeof(enc)) {
				enc[enc_len++] = byte;
			} else {
				overflow = true;
			}
			continue;
		}
		/* delimiter: one complete frame */
		if (enc_len > 0 && !overflow) {
			uint8_t raw[B03_MAX_FRAME];
			struct b03_frame f;
			int n = cobs_decode(enc, enc_len, raw, sizeof(raw));

			if (n < 0) {
				st.rx_bad_cobs++;
			} else if (b03_decode(raw, (size_t)n, &f) != B03_OK) {
				st.rx_bad_crc++;
			} else {
				st.rx_frames++;
				handle_frame(&f);
			}
		} else if (overflow) {
			st.rx_bad_cobs++;
		}
		enc_len = 0;
		overflow = false;
	}
}

static void drain_new_events(void)
{
	struct ev_req r;

	while (k_msgq_get(&ev_q, &r, K_NO_WAIT) == 0) {
		struct pending *slot = NULL;
		struct eaa_status s;

		for (int i = 0; i < MAX_PENDING; i++) {
			if (!pend[i].used) {
				slot = &pend[i];
				break;
			}
		}
		if (slot == NULL) { /* drop the oldest */
			slot = &pend[0];
			for (int i = 1; i < MAX_PENDING; i++) {
				if (pend[i].last_tx_ms < slot->last_tx_ms) {
					slot = &pend[i];
				}
			}
			LOG_WRN("pending events full, dropping event_seq %u", slot->event_seq);
		}
		safety_get_status(&s);
		event_seq = (uint16_t)(event_seq + 1);
		*slot = (struct pending){ .used = true, .event_seq = event_seq, .req = r,
					  .mcu_state = s.state };
		LOG_INF("%s event kind 0x%02x seq %u", r.voice ? "voice" : "mcu", r.kind,
			event_seq);
		send_event(slot);
	}
}

static void retransmit(int64_t now)
{
	for (int i = 0; i < MAX_PENDING; i++) {
		struct pending *p = &pend[i];

		if (!p->used || (now - p->last_tx_ms) < CONFIG_EAA_EVENT_RETX_MS) {
			continue;
		}
		if (p->tries > CONFIG_EAA_EVENT_RETX_MAX) {
			LOG_WRN("event_seq %u never acknowledged", p->event_seq);
			p->used = false;
			continue;
		}
		send_event(p);
		st.retx++;
	}
}

static void bridge_thread(void *a, void *b, void *c)
{
	int64_t next_telem = k_uptime_get();

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		int64_t now = k_uptime_get();
		int64_t wait = next_telem - now;

		(void)k_sem_take(&rx_sem, K_MSEC(CLAMP(wait, 0, 10)));
		process_rx();
		drain_new_events();
		now = k_uptime_get();
		retransmit(now);
		if (now >= next_telem) {
			send_telemetry();
			next_telem += CONFIG_EAA_TELEMETRY_PERIOD_MS;
			if (next_telem < now) {
				next_telem = now + CONFIG_EAA_TELEMETRY_PERIOD_MS;
			}
		}
	}
}

K_THREAD_STACK_DEFINE(bridge_stack, 2048);
static struct k_thread bridge_tid;

int bridge_init(void)
{
	if (!device_is_ready(uart)) {
		LOG_ERR("bridge UART not ready");
		return -ENODEV;
	}
	boot_id = (uint16_t)(k_cycle_get_32() ^ (k_cycle_get_32() >> 16));
	uart_irq_callback_user_data_set(uart, uart_isr, NULL);
	uart_irq_rx_enable(uart);
	return 0;
}

void bridge_start(void)
{
	k_thread_create(&bridge_tid, bridge_stack, K_THREAD_STACK_SIZEOF(bridge_stack),
			bridge_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(3), 0, K_NO_WAIT);
	k_thread_name_set(&bridge_tid, "bridge");
}
