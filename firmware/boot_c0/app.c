/*
 * Test app for the bootloader stress test. One source, variants chosen by the build:
 *   APP_VERSION   "1.0.0" | "2.0.0" | "0.9.0"
 *   APP_SUFFIX    "" | "-crash" | "-hang" | "-noconfirm"   (banner only)
 *   APP_VARIANT_CRASH      HardFault right after the banner (before it can confirm)
 *   APP_VARIANT_HANG       endless loop after the banner, no IWDG feeding
 *   APP_VARIANT_NOCONFIRM  runs normally, never confirms
 *   APP_SLOT      0 (linked for slot A) | 1 (slot B)
 */
#include "app_rt.h"

#ifndef APP_SUFFIX
#define APP_SUFFIX ""
#endif

int main(void) {
    rt_init();
    rt_banner(APP_VERSION APP_SUFFIX);
#if defined(APP_VARIANT_CRASH)
    __asm volatile("udf #1");          /* -> HardFault -> HARDFAULT line -> reset */
#elif defined(APP_VARIANT_HANG)
    for (;;) { }                       /* the BL's IWDG resets us */
#endif
#if defined(APP_VARIANT_NOCONFIRM)
    rt_run(0);
#else
    rt_run(1);
#endif
}
