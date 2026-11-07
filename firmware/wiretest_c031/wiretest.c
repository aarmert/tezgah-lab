/*
 * NUCLEO-C031C6 wiring test (bare metal, no HAL). Not part of the stress system.
 *
 * USART2 PA2/PA3 (ST-LINK VCP), USART1 PA9 TX / PA10 RX (to the ESP32), 115200 8N1.
 * Clock: reset default HSISYS = HSI48/4 = 12 MHz.
 *
 * VCP:    "WT boot csr2=0x........" on every boot (reset flags, then cleared),
 *         "WT u1rx 0x.." for every byte received on USART1.
 * USART1: "WT tick 0x........" every ~1 s, and echoes every received byte back as "E<byte>".
 * LD4 (PA5) toggles with the tick.
 */
#include <stdint.h>

#define REG(a) (*(volatile uint32_t *)(a))

#define RCC        0x40021000u
#define RCC_IOPENR REG(RCC + 0x34)
#define RCC_APBENR1 REG(RCC + 0x3C)
#define RCC_APBENR2 REG(RCC + 0x40)
#define RCC_CSR2   REG(RCC + 0x60)

#define GPIOA      0x50000000u
#define GPIO_MODER REG(GPIOA + 0x00)
#define GPIO_AFRL  REG(GPIOA + 0x20)
#define GPIO_AFRH  REG(GPIOA + 0x24)
#define GPIO_ODR   REG(GPIOA + 0x14)

#define USART1     0x40013800u
#define USART2     0x40004400u
#define U_CR1(b)   REG((b) + 0x00)
#define U_BRR(b)   REG((b) + 0x0C)
#define U_ISR(b)   REG((b) + 0x1C)
#define U_ICR(b)   REG((b) + 0x20)
#define U_RDR(b)   REG((b) + 0x24)
#define U_TDR(b)   REG((b) + 0x28)

extern uint32_t _estack;
void Reset_Handler(void);
static void Default_Handler(void) { for (;;) {} }

__attribute__((section(".isr_vector"), used))
const void *vectors[16] = {
    &_estack, Reset_Handler, Default_Handler, Default_Handler,
};

static void putc_(uint32_t u, char c) {
    while (!(U_ISR(u) & (1u << 7))) {}
    U_TDR(u) = (uint8_t)c;
}
static void puts_(uint32_t u, const char *s) { while (*s) putc_(u, *s++); }
static void hex_(uint32_t u, uint32_t v, int digits) {
    for (int i = digits - 1; i >= 0; i--) putc_(u, "0123456789ABCDEF"[(v >> (4 * i)) & 0xF]);
}

static void uart_init(uint32_t u) {
    U_CR1(u) = 0;
    U_BRR(u) = 104;                 /* 12 MHz / 115200 */
    U_CR1(u) = (1u << 3) | (1u << 2) | 1u;  /* TE | RE | UE */
}

void Reset_Handler(void) {
    RCC_IOPENR |= 1u;               /* GPIOA */
    RCC_APBENR1 |= 1u << 17;        /* USART2 */
    RCC_APBENR2 |= 1u << 14;        /* USART1 */
    (void)RCC_APBENR2;

    uint32_t m = GPIO_MODER;
    m &= ~((3u << 4) | (3u << 6) | (3u << 10) | (3u << 18) | (3u << 20));
    m |= (2u << 4) | (2u << 6) | (1u << 10) | (2u << 18) | (2u << 20);  /* PA2,3,9,10 AF; PA5 out */
    GPIO_AFRL = (GPIO_AFRL & ~((0xFu << 8) | (0xFu << 12))) | (1u << 8) | (1u << 12);  /* AF1 */
    GPIO_AFRH = (GPIO_AFRH & ~((0xFu << 4) | (0xFu << 8))) | (1u << 4) | (1u << 8);    /* AF1 */
    GPIO_MODER = m;

    uart_init(USART2);
    uart_init(USART1);

    uint32_t csr = RCC_CSR2;
    RCC_CSR2 = csr | (1u << 23);    /* RMVF */
    puts_(USART2, "\r\nWT boot csr2=0x"); hex_(USART2, csr, 8); puts_(USART2, "\r\n");
    puts_(USART1, "\r\nWT hello usart1\r\n");

    uint32_t n = 0, loops = 0;
    for (;;) {
        uint32_t isr = U_ISR(USART1);
        if (isr & (1u << 3)) U_ICR(USART1) = 1u << 3;   /* ORE */
        if (isr & (1u << 5)) {
            char c = (char)U_RDR(USART1);
            putc_(USART1, 'E'); putc_(USART1, c);
            puts_(USART2, "WT u1rx 0x"); hex_(USART2, (uint8_t)c, 2); puts_(USART2, "\r\n");
        }
        if (++loops >= 600000u) {   /* roughly 1 s at 12 MHz */
            loops = 0;
            GPIO_ODR ^= 1u << 5;
            puts_(USART1, "WT tick 0x"); hex_(USART1, n++, 8); puts_(USART1, "\r\n");
        }
    }
}
