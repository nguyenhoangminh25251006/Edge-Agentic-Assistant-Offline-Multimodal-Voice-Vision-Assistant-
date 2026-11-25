/* Minimal Zephyr shim so src/safety.c can be unit-tested on a PC (single thread). */
#ifndef SHIM_ZEPHYR_KERNEL_H
#define SHIM_ZEPHYR_KERNEL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>

/* Kconfig values used by safety.c (defaults from firmware/Kconfig) */
#define CONFIG_EAA_HB_TIMEOUT_MS 300
#define CONFIG_EAA_HW_WATCHDOG_MS 0
#define CONFIG_EAA_MIN_SPEED_MMS 50
#define CONFIG_EAA_MAX_SPEED_MMS 500
#define CONFIG_EAA_MAX_CURRENT_MA 2500
#define CONFIG_EAA_OVERCURRENT_FILTER_MS 60
#define CONFIG_EAA_MAX_TEMP_C10 800
#define CONFIG_EAA_JAM_TIMEOUT_MS 3000
#define CONFIG_EAA_REQUIRE_SENSORS 1

#define IS_ENABLED(x) (x)
#define DT_HAS_ALIAS(x) 0
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define CLAMP(v, lo, hi) MIN(MAX(v, lo), hi)
#define ARG_UNUSED(x) (void)(x)
#ifndef INT16_MAX
#define INT16_MAX 32767
#endif

struct k_spinlock { int dummy; };
typedef int k_spinlock_key_t;
static inline k_spinlock_key_t k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static inline void k_spin_unlock(struct k_spinlock *l, k_spinlock_key_t k) { (void)l; (void)k; }

extern int64_t shim_now_ms;
static inline int64_t k_uptime_get(void) { return shim_now_ms; }
static inline void k_sleep(int ms) { (void)ms; }
#define K_MSEC(x) (x)
#define K_NO_WAIT 0
#define K_PRIO_PREEMPT(x) (x)

struct k_thread { int dummy; };
#define K_THREAD_STACK_DEFINE(name, size) static char name[size]
#define K_THREAD_STACK_SIZEOF(s) sizeof(s)
static inline void k_thread_create(void *t, void *s, size_t sz, void *e, void *a, void *b, void *c,
				   int p, int o, int d)
{ (void)t; (void)s; (void)sz; (void)e; (void)a; (void)b; (void)c; (void)p; (void)o; (void)d; }
static inline void k_thread_name_set(void *t, const char *n) { (void)t; (void)n; }

#endif
