/*
 * In-target fault hook (SPEC section 6), compiled in only with -DFAULT_HOOKS.
 *
 * fault_point(PHASE, idx): when an armed noinit entry matches, it is disarmed in noinit first, the
 * start is recorded in noinit.pending (with a new seq), and TIM2 (1 MHz, one-shot) starts with
 * delay_us. The TIM2 ISR runs from RAM (vector table in RAM too), so it fires even while the flash
 * is erasing. It writes the expiry record (noinit.expiry, outside the CRC part, no flash calls)
 * and then does SYSRESETREQ (which also pulses NRST). delay_us = 0 records the expiry and resets on
 * the spot. delay_us = 1 behaves like 2 (TIM2 needs ARR >= 1).
 *
 * A fault counts as *fired* only when its timer expired. A started timer that never expires
 * (OK COMMIT/RESET/IWDG reset first, or FAULT CLEAR) counts as *cancelled*. The BL resolves the
 * last start at entry and reports both on its next boot (fault_boot_snapshot/fault_print_report).
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

/* Records the expiry of the pending timer. RAM-safe: inline loads/stores only, no calls. */
static inline __attribute__((always_inline)) void record_expiry(void) {
    volatile noinit_expiry_t *x = &g_noinit.expiry;
    uint32_t count = expiry_valid(x) ? x->count + 1u : 1u;
    uint32_t seq = g_noinit.pending.seq, phase = g_noinit.pending.phase;
    int32_t index = g_noinit.pending.index;
    uint32_t delay = g_noinit.pending.delay_us;
    x->count = count;
    x->seq = seq;
    x->phase = phase;
    x->index = index;
    x->delay_us = delay;
    x->check = expiry_check(count, seq, phase, index, delay);
    x->magic = EXPIRY_MAGIC;
}

RAMISR void TIM2_IRQHandler(void) {
    TIM2_SR = 0;
    record_expiry();
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
    TIM2_ARR = us > 1u ? us - 1u : 1u;     /* update event after `us` ticks; ARR 0 would never fire */
    TIM2_CNT = 0;
    TIM2_CR1 = CR1_URS;                    /* UG below loads PSC without raising UIF */
    TIM2_EGR = 1u;
    TIM2_SR = 0;
    TIM2_DIER = 1u;                        /* UIE */
    NVIC_IPR_B(TIM2_IRQN) = 0;             /* highest priority */
    NVIC_ISER0 = 1u << TIM2_IRQN;
    TIM2_CR1 = CR1_URS | CR1_OPM | CR1_CEN;
}

static void count_cancelled(void) {
    g_noinit.cancelled.count++;
    g_noinit.cancelled.phase = g_noinit.pending.phase;
    g_noinit.cancelled.index = g_noinit.pending.index;
    g_noinit.cancelled.delay_us = g_noinit.pending.delay_us;
    g_noinit.pending.active = 0;
}

uint32_t fault_point(uint32_t phase, int32_t index) {
    if (!fault_timer_pending()) {
        for (uint32_t e = 0; e < 2u; e++) {
            noinit_fault_t *f = &g_noinit.fault[e];
            if (!f->armed || f->phase != phase || (f->index >= 0 && f->index != index)) continue;
            uint32_t delay = f->delay_us;
            f->armed = 0;                          /* disarm in noinit first */
            g_noinit.pending.active = 1;
            g_noinit.pending.seq++;
            g_noinit.pending.phase = phase;
            g_noinit.pending.index = index;
            g_noinit.pending.delay_us = delay;
            noinit_commit();
            if (delay == 0) {
                __asm volatile("cpsid i");
                record_expiry();
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

static noinit_report_t s_fired, s_cancelled;

void fault_boot_snapshot(void) {
    /* No timer runs at BL entry (a reset stops TIM2): the last started one either expired or not. */
    const volatile noinit_expiry_t *x = &g_noinit.expiry;
    int xv = expiry_valid(x);
    if (g_noinit.pending.active) {
        if (xv && x->count && x->seq == g_noinit.pending.seq) g_noinit.pending.active = 0;  /* fired */
        else count_cancelled();
        noinit_commit();
    }
    s_fired.count = xv ? x->count : 0u;
    s_fired.phase = x->phase;
    s_fired.index = x->index;
    s_fired.delay_us = x->delay_us;
    s_cancelled = g_noinit.cancelled;
}

static void print_report(const char *what, const noinit_report_t *r) {
    uart_puts(what); uart_u32(r->count); uart_putc(' ');
    uart_puts(phase_name(r->phase)); uart_putc(' ');
    uart_i32(r->index); uart_putc(' ');
    uart_u32(r->delay_us); uart_nl();
}

void fault_print_report(void) {
    if (s_fired.count) print_report("BL fault_fired ", &s_fired);
    if (s_cancelled.count) print_report("BL fault_cancelled ", &s_cancelled);
    /* Clear only what was reported: a BOOT fault started after the snapshot may add to it. */
    if (s_fired.count) {
        volatile noinit_expiry_t *x = &g_noinit.expiry;
        __asm volatile("cpsid i");
        if (expiry_valid(x) && x->count >= s_fired.count) {
            x->count -= s_fired.count;
            x->check = expiry_check(x->count, x->seq, x->phase, x->index, x->delay_us);
        }
        __asm volatile("cpsie i");
    }
    if (s_cancelled.count) {
        g_noinit.cancelled.count -= s_cancelled.count;
        noinit_commit();
    }
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
        if (fault_timer_pending()) {
            timer_stop();
            if (g_noinit.pending.active) count_cancelled();
        }
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
void fault_boot_snapshot(void) { }
void fault_print_report(void) { }
int fault_command(const char *line) { (void)line; return 0; }

#endif
