/*
 * Startup for the C0 bootloaders and apps: vector table, .data/.ramfunc copy, .bss zeroing, RAM
 * vector table. The .noinit area (0x20000000, 256 B) is never touched here.
 */
#include "common.h"

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
extern uint32_t _siramfunc, _sramfunc, _eramfunc;
int main(void);

#define VECTOR_COUNT 48u   /* 16 core + 32 STM32C031 IRQs */

/* RAM copy of the vector table. VTOR points here from startup on, so the fault ISR (TIM3), the
 * time base ISR (TIM14) and the app's SysTick (all in RAM) can be taken while the flash is busy
 * (SPEC 5). 48 words -> 256 B alignment. */
__attribute__((section(".ramvec"), aligned(256))) uint32_t g_ram_vectors[VECTOR_COUNT];

static void vectors_to_ram(void) {
    for (uint32_t i = 0; i < VECTOR_COUNT; i++) g_ram_vectors[i] = g_vectors[i];
    __asm volatile("dsb");
    SCB_VTOR = (uint32_t)(uintptr_t)g_ram_vectors;
    __asm volatile("dsb\n isb");
}

void Reset_Handler(void) {
    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) *dst++ = *src++;
    src = &_siramfunc;
    for (uint32_t *dst = &_sramfunc; dst < &_eramfunc;) *dst++ = *src++;
    for (uint32_t *dst = &_sbss; dst < &_ebss;) *dst++ = 0;
    vectors_to_ram();
    /* The BL jumps with interrupts masked (a fault timer may be pending); unmask only now that
     * the RAM vector table and the RAM ISRs exist. */
    __asm volatile("cpsie i");
    main();
    for (;;) { }
}

void Default_Handler(void) { for (;;) { } }

void NMI_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void)     __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void)    __attribute__((weak, alias("Default_Handler")));
void TIM3_IRQHandler(void)    __attribute__((weak, alias("Default_Handler")));
void TIM14_IRQHandler(void)   __attribute__((weak, alias("Default_Handler")));

#define D ((uint32_t)(uintptr_t)Default_Handler)
#define H(f) ((uint32_t)(uintptr_t)(f))

__attribute__((section(".isr_vector"), used))
const uint32_t g_vectors[] = {
    (uint32_t)(uintptr_t)&_estack,
    H(Reset_Handler), H(NMI_Handler), H(HardFault_Handler), 0, 0, 0, 0, 0, 0, 0,
    H(SVC_Handler), 0, 0, H(PendSV_Handler), H(SysTick_Handler),
    /* IRQ 0..15 */
    D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D,
    /* IRQ 16: TIM3 */
    H(TIM3_IRQHandler),
    /* IRQ 17, 18 */
    D, D,
    /* IRQ 19: TIM14 */
    H(TIM14_IRQHandler),
    /* IRQ 20..31 */
    D, D, D, D, D, D, D, D, D, D, D, D,
};
_Static_assert(sizeof g_vectors / sizeof g_vectors[0] == VECTOR_COUNT, "48 vectors");

/* The compiler may emit calls to these for struct copies (no libc) */
void *memcpy(void *d, const void *s, size_t n) {
    uint8_t *dp = d; const uint8_t *sp = s;
    while (n--) *dp++ = *sp++;
    return d;
}

void *memset(void *d, int v, size_t n) {
    uint8_t *dp = d;
    while (n--) *dp++ = (uint8_t)v;
    return d;
}
