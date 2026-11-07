/*
 * Shared app runtime (SPEC sections 3 and 4.3).
 *  - Feeds the IWDG from the SysTick-paced main loop (reloading a stopped IWDG is harmless).
 *  - A trial app (meta trial == own slot) appends CONFIRM after 1 s of healthy running and
 *    prints "APP confirmed".
 *  - Trial deadline: not confirmed after 5 s -> stop feeding, the IWDG resets us.
 *  - Commands: PING VER TICK UID RESET CRASH UPD STATE FAULT, plus OPTR (C0 bring-up: prints
 *    FLASH_OPTR, read only). Accepted on both UARTs, one line buffer each; replies go to both
 *    (SPEC-ESP32 1). A line that overflows or holds a NUL byte -> ERR unknown command.
 */
#include "app_rt.h"
#include "noinit.h"

#define CONFIRM_AFTER_MS   1000u
#define TRIAL_DEADLINE_MS  5000u
#define LINE_MAX_CHARS     96u

static char s_line_u1[LINE_MAX_CHARS + 1u], s_line_u2[LINE_MAX_CHARS + 1u];

static volatile uint32_t g_ms;
static meta_state_t s_meta;
static int s_trial;        /* this boot is a trial of our slot */
static int s_confirmed;

RAMISR void SysTick_Handler(void) { g_ms++; }   /* RAM: also runs while the flash is busy */

/* ---- HardFault: report, then reset (so a trial crash counts as a failed attempt).
 * ARMv6-M has no CFSR/HFSR: zeros keep the host regex (SPEC-C031C6 3). ---- */
__attribute__((used)) void hardfault_report(uint32_t *frame) {
    uart_nl();
    uart_puts("HARDFAULT pc=0x"); uart_hex32(frame[6]);
    uart_puts(" lr=0x");          uart_hex32(frame[5]);
    uart_puts(" cfsr=0x00000000 hfsr=0x00000000");
    uart_nl();
    led_set(1);
    system_reset();
}

/* Thumb-1 only (no IT blocks): r0 = the stack the exception frame was pushed to. */
__attribute__((naked)) void HardFault_Handler(void) {
    __asm volatile(
        "movs r0, #4     \n"
        "mov  r1, lr     \n"
        "tst  r0, r1     \n"
        "mrs  r0, msp    \n"
        "beq  1f         \n"
        "mrs  r0, psp    \n"
        "1:              \n"
        "ldr  r1, =hardfault_report \n"
        "bx   r1         \n"
        ".ltorg          \n");
}

static char slot_letter(uint32_t s) { return s == 0 ? 'A' : (s == 1 ? 'B' : '-'); }

void rt_init(void) {
    hw_init();
    line_init(s_line_u1, s_line_u2, LINE_MAX_CHARS);
    noinit_check();
    SYST_RVR = (SYSCLK_HZ / 1000u) - 1u;
    SYST_CVR = 0;
    SYST_CSR = 7u;
    meta_load(&s_meta);
    s_trial = s_meta.valid && s_meta.rec.trial == (uint8_t)APP_SLOT;
    s_confirmed = 0;
}

void rt_banner(const char *ver) {
    uart_nl();
    uart_puts("BOOT app "); uart_puts(ver);
    uart_puts(" slot="); uart_putc(slot_letter(APP_SLOT));
    uart_puts(" trial="); uart_putc(s_trial ? '1' : '0');
    uart_puts(" reset="); uart_reset_cause(g_noinit.reset_csr);
    uart_nl();
}

static void do_confirm(void) {
    uint32_t t0 = fault_point(PH_CONFIRM, 0);
    iwdg_feed();                      /* before a possible rotation erase (one 2 KB page) */
    meta_load(&s_meta);
    uint32_t own_sc = slot_hdr(APP_SLOT)->security_counter;
    uint32_t min_sc = s_meta.valid ? s_meta.rec.min_security_counter : 0u;
    if (own_sc > min_sc) min_sc = own_sc;
    int e = meta_append(&s_meta, META_CONFIRM, (uint8_t)APP_SLOT, META_NONE, 0, min_sc);
    iwdg_feed();
    fault_trace(PH_CONFIRM, 0, t0);
    if (e) {
        uart_line("APP error META");
        return;
    }
    s_confirmed = 1;
    uart_line("APP confirmed");
}

static char upper(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }

static void handle_command(char *line) {
    for (char *p = line; *p; p++) *p = upper(*p);

    if (str_eq(line, "PING")) {
        uart_line("PONG");
    } else if (str_eq(line, "VER")) {
        uart_line("VER " APP_VERSION);
    } else if (str_eq(line, "TICK")) {
        uart_puts("TICK "); uart_u32(g_ms); uart_nl();
    } else if (str_eq(line, "UID")) {
        uart_puts("UID ");
        for (uint32_t i = 0; i < 3; i++) uart_hex32(REG(UID_BASE + 4u * i));
        uart_nl();
    } else if (str_eq(line, "RESET")) {
        uart_line("OK RESET");
        system_reset();
    } else if (str_eq(line, "CRASH")) {
        uart_line("OK CRASH");
        uart_flush();
        __asm volatile("udf #0");
    } else if (str_eq(line, "UPD")) {
        g_noinit.enter_update = 1;
        noinit_commit();
        uart_line("OK UPD");
        system_reset();
    } else if (str_eq(line, "OPTR")) {
        uart_puts("OPTR 0x"); uart_hex32(FLASH_OPTR); uart_nl();
    } else if (str_eq(line, "STATE")) {
        meta_state_t st;
        meta_load(&st);
        int trial = st.valid && st.rec.trial != META_NONE;
        int confirmed = st.valid && !trial && st.rec.active == (uint8_t)APP_SLOT;
        uart_puts("STATE active="); uart_putc(st.valid ? slot_letter(st.rec.active) : '-');
        uart_puts(" trial="); uart_putc(trial ? '1' : '0');
        uart_puts(" confirmed="); uart_putc(confirmed ? '1' : '0');
        uart_puts(" seq="); uart_u32(st.valid ? st.rec.seq : 0u);
        uart_nl();
    } else if (fault_command(line)) {
        /* handled (hooks builds only) */
    } else {
        uart_line("ERR unknown command");
    }
}

void rt_run(int may_confirm) {
    uint32_t last_toggle = 0;
    uint32_t next_confirm_try = CONFIRM_AFTER_MS;

    for (;;) {
        uint32_t now = g_ms;
        int pending_trial = s_trial && !s_confirmed;

        /* Trial deadline: stop feeding so the IWDG resets an app that never confirms */
        if (!(pending_trial && now >= TRIAL_DEADLINE_MS)) iwdg_feed();

        if (pending_trial && may_confirm && now >= next_confirm_try && now < TRIAL_DEADLINE_MS) {
            do_confirm();
            next_confirm_try = g_ms + 1000u;
        }

        if ((now - last_toggle) >= 250u) {          /* heartbeat on LD4 */
            last_toggle = now;
            led_toggle();
        }

        uint32_t u;
        int n = line_poll(&u);
        if (n > 0) handle_command(line_buf(u));
        else if (n < 0) uart_line("ERR unknown command");   /* overflow / NUL (SPEC-ESP32 1) */
    }
}
