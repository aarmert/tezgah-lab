/*
 * Bootloader in pages 0-5 of the STM32C031 (SPEC sections 3, 4, 4.2, 4.4; SPEC-C031C6).
 *
 *   design=ab    (default): two slots, meta log, trial boot with attempts + IWDG, rollback, repair.
 *   design=naive (-DDESIGN_NAIVE): negative control. Single slot A updated in place, no CRC check
 *                at boot; it jumps to A when payload word 0 looks like a RAM stack pointer.
 *
 * Every boot prints exactly one status line, then the decision line(s).
 */
#include "bl.h"
#include "noinit.h"

#ifdef FAULT_HOOKS
#define HOOKS_WORD " hooks"
#else
#define HOOKS_WORD ""
#endif

#define MAX_ATTEMPTS 3u
#define TARGET_WORD " target=c031"   /* SPEC 3, SPEC-C031C6 3 */

static int s_ok[2];       /* slot passes the full check (ab), or "looks bootable" (naive, A only) */
static uint32_t s_boot_t0;

/* ---- HardFault in the BL: report and stop (a BL fault must be visible, not a boot loop).
 * ARMv6-M has no CFSR/HFSR: the line prints zeros there to keep the host regex (SPEC-C031C6 3). */
__attribute__((used)) void hardfault_report(uint32_t *frame) {
    uart_nl();
    uart_puts("HARDFAULT pc=0x"); uart_hex32(frame[6]);
    uart_puts(" lr=0x");          uart_hex32(frame[5]);
    uart_puts(" cfsr=0x00000000 hfsr=0x00000000");
    uart_nl();
    led_set(1);
    for (;;) { }
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

/* ---- Jump to an app payload ---- */
__attribute__((noreturn)) static void jump_to(uint32_t slot) {
    uint32_t t0 = fault_point(PH_JUMP, (int32_t)slot);
    fault_trace(PH_JUMP, (int32_t)slot, t0);
    uart_flush();

    uint32_t base = PAYLOAD_ADDR(slot);
    uint32_t sp = ((const volatile uint32_t *)base)[0];
    uint32_t pc = ((const volatile uint32_t *)base)[1];

    /* Interrupts stay masked until the app's startup has its RAM vector table and RAM ISRs;
     * the app unmasks them. A pending fault timer (TIM3, its state in noinit.ftimer) keeps
     * running into the app. The TIM14 time base stops here; the app starts its own. */
    __asm volatile("cpsid i");
    if (!fault_timer_pending()) {
        TIM_CR1(TIM3_BASE) = 0;
        TIM_DIER(TIM3_BASE) = 0;
        NVIC_ICER0 = 1u << TIM3_IRQN;
        NVIC_ICPR0 = 1u << TIM3_IRQN;
    }
    timebase_stop();
    SYST_CSR = 0;
    SCB_VTOR = base;
    __asm volatile("dsb\n isb");
    __asm volatile(
        "msr msp, %0 \n"
        "bx %1       \n" : : "r"(sp), "r"(pc) : "memory");
    __builtin_unreachable();
}

static void print_status_common_tail(uint32_t reset_csr) {
    uart_puts(" reset=");
    uart_reset_cause(reset_csr);
    uart_nl();
}

#ifndef DESIGN_NAIVE
/* =========================== A/B =========================== */
meta_state_t g_meta;

static void refresh(void) {
    for (uint32_t s = 0; s < 2u; s++) s_ok[s] = img_check_slot(s) == IMG_OK;
    meta_load(&g_meta);
}

static void print_slot(uint32_t s) {
    if (s_ok[s]) { uart_puts("ok:"); uart_version(slot_hdr(s)->fw_version); }
    else uart_puts(slot_is_empty(s) ? "empty" : "bad");
}

static char slot_letter(uint32_t s) { return s == 0 ? 'A' : (s == 1 ? 'B' : '-'); }

static void print_status(void) {
    const meta_rec_t *r = &g_meta.rec;
    uart_puts("BL v0.1 design=" BL_DESIGN HOOKS_WORD TARGET_WORD " slotA="); print_slot(0);
    uart_puts(" slotB="); print_slot(1);
    uart_puts(" active="); uart_putc(g_meta.valid ? slot_letter(r->active) : '-');
    uart_puts(" state=");
    uart_puts(!g_meta.valid ? "none" : (r->trial != META_NONE ? "trial" : "confirmed"));
    uart_puts(" attempts="); uart_u32(g_meta.valid ? r->attempts : 0u);
    uart_puts(" seq="); uart_u32(g_meta.valid ? r->seq : 0u);
    uart_puts(" erases="); uart_u32(g_meta.erases[0]); uart_putc(','); uart_u32(g_meta.erases[1]);
    print_status_common_tail(g_noinit.reset_csr);
}

static void meta_err(int e) { if (e) uart_line("BL error META"); }

static void boot_decision(void) {
    for (;;) {
        /* 1. No valid meta record: boot whichever slot is valid (A first) and record it */
        if (!g_meta.valid) {
            int pick = s_ok[0] ? 0 : (s_ok[1] ? 1 : -1);
            if (pick < 0) {
                uart_line("BL nofw");
                bl_update_mode(UPD_NOFW);
                refresh();
                continue;
            }
            meta_err(meta_append(&g_meta, META_CONFIRMED, (uint8_t)pick, META_NONE, 0,
                                 slot_hdr((uint32_t)pick)->security_counter));
        }
        /* 2. noinit flags */
        if (g_noinit.enter_update) {
            g_noinit.enter_update = 0;
            noinit_commit();
            uart_line("BL update");
            bl_update_mode(UPD_NORMAL);
            refresh();
            continue;
        }
        if (g_noinit.update_in_progress) {
            uart_line("BL resume");
            bl_update_mode(UPD_RESUME);
            refresh();
            continue;
        }
        meta_rec_t r = g_meta.rec;
        /* 3. Trial pending */
        if (g_meta.valid && r.trial != META_NONE) {
            uint32_t t = r.trial;
            if (t > 1u || r.attempts >= MAX_ATTEMPTS || !s_ok[t]) {
                meta_err(meta_append(&g_meta, META_ROLLBACK, r.active, META_NONE, r.attempts,
                                     r.min_security_counter));
                uart_puts("BL rollback "); uart_putc(slot_letter(t)); uart_puts("->");
                uart_putc(slot_letter(r.active)); uart_nl();
                r = g_meta.rec;
            } else {
                int e = meta_append(&g_meta, META_ATTEMPT, r.active, (uint8_t)t,
                                    (uint8_t)(r.attempts + 1u), r.min_security_counter);
                meta_err(e);
                if (!e) {
                    iwdg_start_2s();
                    uart_puts("BL jump "); uart_putc(slot_letter(t)); uart_puts(" trial"); uart_nl();
                    jump_to(t);
                }
                /* Could not count the attempt: do not run an uncounted trial, boot active. */
            }
        }
        /* 4. Boot active, or repair to the other valid slot */
        uint32_t a = r.active;
        if (a <= 1u && s_ok[a]) {
            uart_puts("BL jump "); uart_putc(slot_letter(a)); uart_nl();
            jump_to(a);
        }
        int other = (a <= 1u) ? (s_ok[1u - a] ? (int)(1u - a) : -1) : (s_ok[0] ? 0 : (s_ok[1] ? 1 : -1));
        if (other >= 0) {
            meta_err(meta_append(&g_meta, META_REPAIR, (uint8_t)other, META_NONE, 0,
                                 r.min_security_counter));
            uart_puts("BL repair active="); uart_putc(slot_letter((uint32_t)other)); uart_nl();
            uart_puts("BL jump "); uart_putc(slot_letter((uint32_t)other)); uart_nl();
            jump_to((uint32_t)other);
        }
        uart_line("BL nofw");
        bl_update_mode(UPD_NOFW);
        refresh();
    }
}

#else
/* =========================== naive =========================== */

/* Deliberately weak: "bootable" = payload word 0 looks like a RAM stack pointer. No CRC. */
static int naive_bootable(void) {
    uint32_t sp = *(const volatile uint32_t *)PAYLOAD_ADDR(0);
    return sp > RAM_BASE && sp <= RAM_BASE + RAM_SIZE;
}

static void refresh(void) { s_ok[0] = naive_bootable(); s_ok[1] = 0; }

static void print_status(void) {
    const img_hdr_t *h = slot_hdr(0);
    uart_puts("BL v0.1 design=" BL_DESIGN HOOKS_WORD TARGET_WORD " slotA=");
    /* naive trusts the header magic for the status; it never checks the payload CRC */
    if (h->magic == IMG_MAGIC) { uart_puts("ok:"); uart_version(h->fw_version); }
    else uart_puts(slot_is_empty(0) ? "empty" : "bad");
    uart_puts(" slotB=- active="); uart_putc(s_ok[0] ? 'A' : '-');
    uart_puts(" state=none attempts=0 seq=0 erases=0,0");
    print_status_common_tail(g_noinit.reset_csr);
}

static void boot_decision(void) {
    for (;;) {
        if (g_noinit.enter_update) {
            g_noinit.enter_update = 0;
            noinit_commit();
            uart_line("BL update");
            bl_update_mode(UPD_NORMAL);
            refresh();
            continue;
        }
        /* naive: no resume (SPEC 4) - an interrupted in-place update is not repaired */
        if (s_ok[0]) {
            uart_line("BL jump A");
            jump_to(0);
        }
        uart_line("BL nofw");
        bl_update_mode(UPD_NOFW);
        refresh();
    }
}
#endif

static char s_line_u1[UPD_LINE_MAX + 1u], s_line_u2[UPD_LINE_MAX + 1u];

int main(void) {
    hw_init();
    line_init(s_line_u1, s_line_u2, UPD_LINE_MAX);
    noinit_check();
    fault_boot_snapshot();                  /* before BOOT: a BOOT fault must not lose it */
    s_boot_t0 = fault_point(PH_BOOT, 0);   /* BL entry */

    uint32_t csr = RCC_CSR2;
    RCC_CSR2 |= CSR2_RMVF;                  /* clear the reset flags */
    g_noinit.reset_csr = csr;
    noinit_commit();

    refresh();
    uart_nl();
    print_status();
    fault_print_report();
    fault_trace(PH_BOOT, 0, s_boot_t0);     /* T BOOT 0 <us from BL entry to the decision> */
    boot_decision();
    for (;;) { }
}
