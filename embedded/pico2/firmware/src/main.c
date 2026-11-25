/* EAA MCU firmware - Raspberry Pi Pico 2 (RP2350, Cortex-M33, Zephyr) as a stand-in
 * for the STM32U585 MCU of the Arduino UNO Q (CH-235). Single core, Zephyr, same
 * portable APIs as the UNO Q MCU; only the devicetree overlay and voice/i2s_in_rp2.c
 * are board-specific.
 *
 * Threads (higher first):  control 200 Hz (coop) > safety 100 Hz > bridge > sensors 50 Hz
 *                          > voice / keyword spotter > shell / logging
 */
#include <stdlib.h>
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <zephyr/shell/shell.h>
#include <zephyr/usb/usb_device.h>
#include "eaa.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

static const struct gpio_dt_spec status_led = GPIO_DT_SPEC_GET_OR(DT_ALIAS(led0), gpios, {0});

static void fail(const char *what, int rc)
{
	LOG_ERR("%s init failed (%d) - motor stays disabled", what, rc);
}

int main(void)
{
	int rc;

	if (IS_ENABLED(CONFIG_USB_DEVICE_STACK)) {
		(void)usb_enable(NULL); /* CDC ACM: console + audio channel */
	}
	k_sleep(K_MSEC(200));
	LOG_INF("EAA MCU fw 0x%04x, B-03 v%u, board %s", CONFIG_EAA_FW_VERSION, B03_PROTO_VER,
		CONFIG_BOARD);

	rc = motor_init(); /* first: driver disabled, PWM 0 */
	if (rc) {
		fail("motor", rc);
		return rc;
	}
	if ((rc = encoder_init())) {
		fail("encoder", rc);
	}
	if ((rc = sensors_init())) {
		fail("sensors", rc);
	}
	safety_init();
	if ((rc = bridge_init())) {
		fail("bridge", rc);
	}

	sensors_start();
	safety_start();
	control_start();
	bridge_start();
#if IS_ENABLED(CONFIG_EAA_VOICE)
	if ((rc = voice_init())) {
		fail("microphone", rc);
	}
	voice_start();
#endif

	if (status_led.port) {
		gpio_pin_configure_dt(&status_led, GPIO_OUTPUT_INACTIVE);
	}
	for (;;) { /* heartbeat LED: slow = OK, fast = SAFE_STOP */
		struct eaa_status s;

		safety_get_status(&s);
		if (status_led.port) {
			gpio_pin_toggle_dt(&status_led);
		}
		k_sleep(K_MSEC(s.state == B03_ST_SAFE_STOP ? 100 : 500));
	}
	return 0;
}

/* ---------------- shell: bench tests without the Pi ---------------- */
static const char *state_name(uint8_t s)
{
	static const char *const n[] = { "BOOT", "STOPPED", "RUNNING", "SAFE_STOP" };

	return s < ARRAY_SIZE(n) ? n[s] : "?";
}

static int cmd_status(const struct shell *sh, size_t argc, char **argv)
{
	struct eaa_status s;
	struct eaa_sensors x;
	struct eaa_control_stats c;
	struct eaa_bridge_stats b;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	safety_get_status(&s);
	sensors_get(&x);
	control_get_stats(&c, false);
	bridge_get_stats(&b);
	shell_print(sh, "state %s fault 0x%02x flags 0x%02x setpoint %u mm/s", state_name(s.state),
		    s.fault_reason, s.flags, s.setpoint_mms);
	shell_print(sh, "speed %u mm/s pwm %u/1000", control_speed_mms(), control_pwm_pm());
	char temp[24];

	if (x.temp_c10 == B03_TEMP_NOT_INSTALLED) {
		snprintk(temp, sizeof(temp), "not fitted");
	} else if (x.temp_c10 == B03_TEMP_FAULT) {
		snprintk(temp, sizeof(temp), "FAULT");
	} else {
		snprintk(temp, sizeof(temp), "%d.%d C", x.temp_c10 / 10, abs(x.temp_c10 % 10));
	}
	shell_print(sh, "estop %s, beam %s, current %u mA (%s), bus %u mV, temp %s, ss 0x%02x",
		    x.estop_open ? "OPEN" : "closed", x.pe_blocked ? "BLOCKED" : "clear",
		    x.current_ma,
		    !IS_ENABLED(CONFIG_EAA_INA226) ? "not fitted" : (x.current_ok ? "ok" : "FAULT"),
		    x.bus_mv, temp, x.status);
	shell_print(sh, "control loops %u, jitter max %u us, exec max %u us", c.loops,
		    c.jitter_max_us, c.exec_max_us);
	shell_print(sh, "link %s rx %u bad_crc %u bad_cobs %u tx %u retx %u pending %u",
		    b.synced ? "synced" : "no HELLO yet", b.rx_frames, b.rx_bad_crc, b.rx_bad_cobs,
		    b.tx_frames, b.retx, b.events_pending);
	shell_print(sh, "uptime %u ms, hw watchdog %s (%d ms), board %s", k_uptime_get_32(),
		    safety_watchdog_active() ? "ON" : "off", CONFIG_EAA_HW_WATCHDOG_MS, CONFIG_BOARD);
	return 0;
}

static int cmd_wdtest(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	shell_print(sh, "simulating a firmware hang: interrupts off, spinning. "
			"The hardware watchdog must reset the chip within %d ms.",
		    CONFIG_EAA_HW_WATCHDOG_MS);
	k_sleep(K_MSEC(50));
	(void)irq_lock();
	for (;;) {
	}
	return 0;
}

static int cmd_jitter_reset(const struct shell *sh, size_t argc, char **argv)
{
	struct eaa_control_stats c;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	control_get_stats(&c, true);
	shell_print(sh, "jitter stats reset");
	return 0;
}

static int local_cmd(const struct shell *sh, uint8_t type, uint16_t sp)
{
	static uint16_t id = 0xF000; /* local ids, away from the MPU's range */
	struct b03_cmd c = { .type = type, .cmd_id = id++, .ttl_ms = 500, .setpoint_mms = sp };
	uint8_t reason;
	uint8_t res;

	safety_on_hello(); /* bench mode: act as if the MPU were connected */
	res = safety_handle_cmd(&c, &reason);
	shell_print(sh, "result %u reason 0x%02x", res, reason);
	return 0;
}

static int cmd_run(const struct shell *sh, size_t argc, char **argv)
{
	return local_cmd(sh, B03_MT_RUN, argc > 1 ? (uint16_t)atoi(argv[1]) : 150);
}

static int cmd_stop(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	return local_cmd(sh, B03_MT_STOP, 0);
}

static int cmd_clear(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	return local_cmd(sh, B03_MT_CLEAR_FAULT, 0);
}

static int cmd_hb(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	safety_on_heartbeat();
	shell_print(sh, "heartbeat (valid for %d ms)", CONFIG_EAA_HB_TIMEOUT_MS);
	return 0;
}

#if IS_ENABLED(CONFIG_EAA_VOICE)
static int cmd_wake(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	voice_simulate_wake();
	shell_print(sh, "simulated wake word");
	return 0;
}

static int cmd_vstop(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	voice_simulate_stop();
	shell_print(sh, "simulated spoken stop");
	return 0;
}
#endif

#if defined(CONFIG_SOC_SERIES_RP2350)
#include <pico/bootrom.h>

static int cmd_bootsel(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	shell_print(sh, "rebooting into the USB bootloader (drive RP2350)...");
	motor_apply(false, 0);
	k_sleep(K_MSEC(100));
	unsigned int key = irq_lock();

	ARG_UNUSED(key);
	safety_watchdog_disable(); /* else the armed watchdog resets the chip out of BOOTSEL */
	reset_usb_boot(0, 0);
	return 0;
}
#endif

SHELL_STATIC_SUBCMD_SET_CREATE(eaa_cmds,
#if defined(CONFIG_SOC_SERIES_RP2350)
	SHELL_CMD(bootsel, NULL, "Reboot into the UF2 bootloader (re-flash without the button)",
		  cmd_bootsel),
#endif
	SHELL_CMD(status, NULL, "State, sensors, loop jitter, link counters", cmd_status),
	SHELL_CMD(jreset, NULL, "Reset jitter statistics", cmd_jitter_reset),
	SHELL_CMD(wdtest, NULL, "Hang the firmware on purpose (watchdog reset test)", cmd_wdtest),
	SHELL_CMD_ARG(run, NULL, "run [mm/s]  (bench only: needs 'eaa hb' within 300 ms)",
		      cmd_run, 1, 1),
	SHELL_CMD(stop, NULL, "Controlled stop", cmd_stop),
	SHELL_CMD(clear, NULL, "CLEAR_FAULT", cmd_clear),
	SHELL_CMD(hb, NULL, "Send one local heartbeat", cmd_hb),
#if IS_ENABLED(CONFIG_EAA_VOICE)
	SHELL_CMD(wake, NULL, "Simulate the wake word", cmd_wake),
	SHELL_CMD(vstop, NULL, "Simulate spoken 'stop'", cmd_vstop),
#endif
	SHELL_SUBCMD_SET_END);
SHELL_CMD_REGISTER(eaa, &eaa_cmds, "EAA MCU bench commands", NULL);
