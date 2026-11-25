/* Module interfaces of the EAA MCU firmware.
 *
 * Ownership rules (the safety rule "the MPU proposes, the MCU decides"):
 *  - safety.c is the only module that decides whether the motor may run.
 *  - control.c is the only module that writes the PWM / enable outputs.
 *  - bridge.c is the only module that transmits on the B-03 UART.
 * Every module uses portable Zephyr APIs (GPIO, PWM, I2C, UART, watchdog);
 * the only board-specific file is voice/i2s_in_rp2.c (PIO microphone capture).
 */
#ifndef EAA_H
#define EAA_H
#include <stdbool.h>
#include <stdint.h>
#include "proto/b03.h"

/* sensor_status bits sent in TELEMETRY (0 = all good). To be confirmed against
 * B-03 section 4.3, which the orchestrator code does not define yet. */
#define EAA_SS_CURRENT_FAULT  (1u << 0)
#define EAA_SS_TEMP_FAULT     (1u << 1)
#define EAA_SS_ENCODER_ABSENT (1u << 2)
#define EAA_SS_PE_BLOCKED     (1u << 3)
#define EAA_SS_MIC_FAULT      (1u << 4)

/* ---- sensors.c ---- */
struct eaa_sensors {
	bool estop_open;     /* E-stop auxiliary contact open */
	bool pe_blocked;     /* photoelectric beam blocked */
	bool current_ok;     /* INA226 fitted and answering (true if not fitted) */
	bool temp_ok;        /* MLX90614 fitted and answering (true if not fitted) */
	uint16_t current_ma;
	int16_t temp_c10;    /* 0x7FFF not installed, 0x7FFE fault */
	uint16_t bus_mv;
	uint8_t status;      /* EAA_SS_* bits */
};
int sensors_init(void);
void sensors_start(void);
void sensors_get(struct eaa_sensors *out);
bool sensors_estop_open_now(void); /* direct pin read, for the control loop */

/* ---- motor.c ---- */
int motor_init(void);
void motor_apply(bool enable, uint16_t pwm_pm);

/* ---- encoder.c ---- */
int encoder_init(void);
bool encoder_fitted(void);
/* Signed counts since the previous call. */
int32_t encoder_take_counts(void);

/* ---- control.c ---- */
struct eaa_control_stats {
	uint32_t loops;
	uint32_t jitter_max_us;  /* worst |period - nominal| since reset */
	uint32_t exec_max_us;    /* worst loop body time */
};
void control_start(void);
uint16_t control_speed_mms(void);
uint16_t control_pwm_pm(void);
void control_get_stats(struct eaa_control_stats *out, bool reset);

/* ---- safety.c ---- */
struct eaa_status {
	uint8_t state;        /* enum b03_mcu_state */
	uint8_t fault_reason; /* enum b03_reason */
	uint8_t flags;        /* B03_FLAG_* */
	uint16_t setpoint_mms;
};
int safety_init(void);
void safety_start(void);
void safety_get_status(struct eaa_status *out);
/* What the control loop must do now. hard_stop = cut PWM immediately. */
void safety_target(bool *run, uint16_t *setpoint_mms, bool *hard_stop);
void safety_on_hello(void);
void safety_on_heartbeat(void);
/* Validates a motion command; returns B03_ACK_* and sets *reason. */
uint8_t safety_handle_cmd(const struct b03_cmd *cmd, uint8_t *reason);
/* Voice front end */
void safety_voice_stop(void);
void safety_set_voice_active(bool active);
void safety_set_mic_ok(bool ok);

/* ---- bridge.c ---- */
int bridge_init(void);
void bridge_start(void);
/* Thread-safe: queue an event; bridge.c adds event_seq, retransmits until EVENT_ACK. */
void bridge_emit_event(uint8_t kind, uint8_t reason, int16_t observed, int16_t threshold);
void bridge_emit_voice(uint8_t kind, uint8_t confidence_pct, uint16_t model_ver);
struct eaa_bridge_stats {
	uint32_t rx_frames, rx_bad_crc, rx_bad_cobs, tx_frames, events_pending, retx;
	bool synced;
};
void bridge_get_stats(struct eaa_bridge_stats *out);

/* ---- voice/voice.c ---- */
int voice_init(void);
void voice_start(void);
void voice_simulate_wake(void);
void voice_simulate_stop(void);

#endif
