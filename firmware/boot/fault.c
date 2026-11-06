/*
 * In-target fault hook (SPEC section 6), compiled in only with -DFAULT_HOOKS.
 *
 * fault_point(PHASE, idx): when an armed noinit entry matches, it is disarmed in noinit first,
 * fired_count/last_fired are updated, and TIM2 (1 MHz, one-shot) starts with delay_us. The TIM2
 * ISR runs from RAM (vector table in RAM too), so it fires even while the flash is erasing, and
 * does SYSRESETREQ (which also pulses NRST). delay_us = 0 resets on the spot.
 *
 * Only one fault timer can run at a time: while it is pending, other armed entries stay armed (so
 * entry 1 survives into the next boot: double fault). A pending timer keeps running across phases
 * and across the BL -> app jump (the app's RAM ISR takes it over).
 */
#include "common.h"
#include "noinit.h"

static const char *const PHASES[PH_COUNT] = {
    "ERASE", "DATA", "HDR", "META", "ROTATE", "BOOT", "JUMP", "CONFIRM",
};

const char *phase_name(uint32_t p) { return p < PH_COUNT ? PHASES[p] : "?"; }

#ifdef FAULT_HOOKS

#define CR1_CEN (1u << 0)
#define CR1_URS (1u << 2)
#define CR1_OPM (1u << 3)

RAMISR void TIM2_IRQHandler(void) {
    TIM2_SR = 0;
    __asm volatile("dsb");
    SCB_AIRCR = AIRCR_SYSRESETREQ;
    __asm volatile("dsb");
    for (;;) { }
}

int fault_timer_pending(void) { return (TIM2_CR1 & CR1_CEN) != 0; }

static void timer_stop(void) {
    TIM2_CR1 = 0;
    TIM2_DIER = 0;
    TIM2_SR = 0;
    NVIC_ICER0 = 1u << TIM2_IRQN;
    NVIC_ICPR0 = 1u << TIM2_IRQN;
}

static void timer_start(uint32_t us) {
    RCC_APB1ENR |= 1u << 0;
    (void)RCC_APB1ENR;
    timer_stop();
    TIM2_PSC = (HSI_HZ / 1000000u) - 1u;   /* 1 MHz */
    TIM2_ARR = us - 1u;                    /* update event after `us` ticks */
    TIM2_CNT = 0;
    TIM2_CR1 = CR1_URS;                    /* UG below loads PSC without raising UIF */
    TIM2_EGR = 1u;
    TIM2_SR = 0;
    TIM2_DIER = 1u;                        /* UIE */
    NVIC_IPR_B(TIM2_IRQN) = 0;             /* highest priority */
    NVIC_ISER0 = 1u << TIM2_IRQN;
    TIM2_CR1 = CR1_URS | CR1_OPM | CR1_CEN;
}

uint32_t fault_point(uint32_t phase, int32_t index) {
    if (!fault_timer_pending()) {
        for (uint32_t e = 0; e < 2u; e++) {
            noinit_fault_t *f = &g_noinit.fault[e];
            if (!f->armed || f->phase != phase || (f->index >= 0 && f->index != index)) continue;
            uint32_t delay = f->delay_us;
            f->armed = 0;                          /* disarm in noinit first */
            g_noinit.fired_count++;
            g_noinit.last_fired.phase = phase;
            g_noinit.last_fired.index = index;
            g_noinit.last_fired.delay_us = delay;
            noinit_commit();
            if (delay == 0) {
                __asm volatile("dsb");
                SCB_AIRCR = AIRCR_SYSRESETREQ;
                __asm volatile("dsb");
                for (;;) { }
            }
            timer_start(delay);
            break;
        }
    }
    return dwt_now();
}

void fault_trace(uint32_t phase, int32_t index, uint32_t t0) {
    uint32_t us = dwt_us_since(t0);
    if (!g_noinit.trace) return;
    uart_puts("T "); uart_puts(phase_name(phase)); uart_putc(' ');
    uart_i32(index); uart_putc(' '); uart_u32(us); uart_nl();
}

static struct { uint32_t count, phase, delay; int32_t index; } s_fired;

void fault_snapshot_fired(void) {
    s_fired.count = g_noinit.fired_count;
    s_fired.phase = g_noinit.last_fired.phase;
    s_fired.index = g_noinit.last_fired.index;
    s_fired.delay = g_noinit.last_fired.delay_us;
    if (g_noinit.fired_count) {
        g_noinit.fired_count = 0;
        noinit_commit();
    }
}

void fault_print_fired(void) {
    if (!s_fired.count) return;
    uart_puts("BL fault_fired "); uart_u32(s_fired.count); uart_putc(' ');
    uart_puts(phase_name(s_fired.phase)); uart_putc(' ');
    uart_i32(s_fired.index); uart_putc(' ');
    uart_u32(s_fired.delay); uart_nl();
}

static int phase_from(const char *s, uint32_t len) {
    for (uint32_t p = 0; p < PH_COUNT; p++) {
        const char *n = PHASES[p];
        uint32_t i = 0;
        while (i < len && n[i] && n[i] == s[i]) i++;
        if (i == len && n[i] == '\0') return (int)p;
    }
    return -1;
}

static void list_entry(uint32_t e) {
    const noinit_fault_t *f = &g_noinit.fault[e];
    uart_puts("FAULT "); uart_u32(e); uart_putc(' ');
    if (!f->armed) { uart_putc('-'); uart_nl(); return; }
    uart_puts(phase_name(f->phase)); uart_putc(' ');
    uart_i32(f->index); uart_putc(' ');
    uart_u32(f->delay_us); uart_nl();
}

int fault_command(const char *line) {
    if (!(str_eq(line, "FAULT") || starts_with(line, "FAULT "))) return 0;
    const char *p = line + 5;
    if (*p == ' ') p++;

    if (starts_with(p, "ARM ")) {
        p += 4;
        uint32_t n, delay, len;
        int32_t idx;
        if (!parse_u32(&p, &n) || n > 1u) goto bad;
        const char *ph = next_token(&p, &len);
        int phase = phase_from(ph, len);
        if (phase < 0) goto bad;
        if (!parse_i32(&p, &idx) || idx < -1) goto bad;
        if (!parse_u32(&p, &delay)) goto bad;
        /* Optional fault action; "reset" is the default and the only one (SPEC 6.1). */
        if (*p && !str_eq(p, "reset") && !str_eq(p, "RESET")) goto bad;
        noinit_fault_t *f = &g_noinit.fault[n];
        f->phase = (uint32_t)phase;
        f->index = idx;
        f->delay_us = delay;
        f->armed = 1;
        noinit_commit();
        uart_puts("OK FAULT ARM "); uart_u32(n); uart_nl();
        return 1;
    }
    if (str_eq(p, "CLEAR")) {
        timer_stop();
        g_noinit.fault[0].armed = 0;
        g_noinit.fault[1].armed = 0;
        noinit_commit();
        uart_line("OK FAULT CLEAR");
        return 1;
    }
    if (str_eq(p, "LIST")) {
        list_entry(0);
        list_entry(1);
        return 1;
    }
    if (str_eq(p, "TRACE ON") || str_eq(p, "TRACE OFF")) {
        g_noinit.trace = str_eq(p, "TRACE ON");
        noinit_commit();
        uart_line("OK FAULT TRACE");
        return 1;
    }
bad:
    uart_line("ERR FAULT");
    return 1;
}

#else  /* !FAULT_HOOKS: release build, no fault points, no FAULT commands, no trace */

int fault_timer_pending(void) { return 0; }
uint32_t fault_point(uint32_t phase, int32_t index) { (void)phase; (void)index; return dwt_now(); }
void fault_trace(uint32_t phase, int32_t index, uint32_t t0) { (void)phase; (void)index; (void)t0; }
void fault_snapshot_fired(void) { }
void fault_print_fired(void) { }
int fault_command(const char *line) { (void)line; return 0; }

#endif
