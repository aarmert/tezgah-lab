/*
 * Clock (HSI48 -> 48 MHz), GPIO, USART1 + USART2, TIM14 time base, IWDG, reset, string helpers.
 *
 * Two UARTs (SPEC-ESP32 1): USART1 PA9/PA10 is the update channel to the ESP32, USART2 PA2/PA3 is
 * the ST-LINK VCP witness. Every output byte goes to both, in the same order. Input is accepted on
 * both, one line buffer per UART; received bytes are moved into a small ring per UART whenever the
 * firmware polls or waits to transmit, so a byte that arrives during a reply is not lost.
 */
#include "common.h"

static const uint32_t UBASE[NUM_UARTS] = {USART1_BASE, USART2_BASE};

/* ---- Clock ---- */
static void clock_init(void) {
    /* 1 wait state BEFORE leaving 12 MHz (0 WS is only allowed up to 24 MHz) */
    uint32_t acr = FLASH_ACR;
    if ((acr & ACR_LATENCY_MSK) != 1u) {
        FLASH_ACR = (acr & ~ACR_LATENCY_MSK) | 1u;
        while ((FLASH_ACR & ACR_LATENCY_MSK) != 1u) { }
    }
    /* SYSCLK = HSISYS (reset default SW = 0) = HSI48 / HSIDIV; HSIDIV = /1. AHB and APB stay /1. */
    if (RCC_CR & RCC_CR_HSIDIV_MSK) {
        RCC_CR &= ~RCC_CR_HSIDIV_MSK;
        while (!(RCC_CR & RCC_CR_HSIRDY)) { }
    }
}

void nvic_set_prio(uint32_t irq, uint32_t prio) {
    uint32_t sh = (irq & 3u) * 8u;
    uint32_t v = NVIC_IPR(irq);                     /* word access only on ARMv6-M */
    v &= ~(0xFFu << sh);
    v |= ((prio & 3u) << 6) << sh;
    NVIC_IPR(irq) = v;
}

/* ---- Time base: TIM14 at 1 MHz, free running, overflow count in RAM ---- */
static volatile uint32_t s_ovf;

RAMISR void TIM14_IRQHandler(void) {
    TIM_SR(TIM14_BASE) = ~TIM_SR_UIF;               /* rc_w0: only UIF is cleared */
    s_ovf++;
}

static void timebase_init(void) {
    RCC_APBENR2 |= RCC_APBENR2_TIM14;
    (void)RCC_APBENR2;
    TIM_CR1(TIM14_BASE) = 0;
    TIM_DIER(TIM14_BASE) = 0;
    NVIC_ICER0 = 1u << TIM14_IRQN;
    NVIC_ICPR0 = 1u << TIM14_IRQN;
    s_ovf = 0;
    TIM_PSC(TIM14_BASE) = SYSCLK_HZ / 1000000u - 1u;
    TIM_ARR(TIM14_BASE) = 0xFFFFu;
    TIM_CNT(TIM14_BASE) = 0;
    TIM_CR1(TIM14_BASE) = TIM_CR1_URS;              /* UG loads PSC without raising UIF */
    TIM_EGR(TIM14_BASE) = 1u;
    TIM_SR(TIM14_BASE) = 0;
    TIM_DIER(TIM14_BASE) = 1u;                      /* UIE */
    nvic_set_prio(TIM14_IRQN, 1);                   /* below the fault timer (0) */
    NVIC_ISER0 = 1u << TIM14_IRQN;
    TIM_CR1(TIM14_BASE) = TIM_CR1_URS | TIM_CR1_CEN;
}

void timebase_stop(void) {
    TIM_CR1(TIM14_BASE) = 0;
    TIM_DIER(TIM14_BASE) = 0;
    NVIC_ICER0 = 1u << TIM14_IRQN;
    NVIC_ICPR0 = 1u << TIM14_IRQN;
}

uint32_t time_us(void) {
    uint32_t pm;
    __asm volatile("mrs %0, primask" : "=r"(pm));
    __asm volatile("cpsid i" ::: "memory");
    uint32_t hi = s_ovf;
    uint32_t lo = TIM_CNT(TIM14_BASE);
    if (TIM_SR(TIM14_BASE) & TIM_SR_UIF) {          /* wrapped, ISR not yet run */
        lo = TIM_CNT(TIM14_BASE);
        hi++;
    }
    __asm volatile("msr primask, %0" : : "r"(pm) : "memory");
    return (hi << 16) | (lo & 0xFFFFu);
}

void delay_ms(uint32_t ms) {
    uint32_t t0 = time_us();
    while (us_since(t0) < ms * 1000u) uart_rx_service();
}

/* ---- GPIO + USARTs ---- */
static void usart_init(uint32_t b) {
    uint32_t brr = (SYSCLK_HZ + BAUD / 2u) / BAUD;   /* 417 */
    if (U_BRR(b) != brr || (U_CR1(b) & (CR1_UE | CR1_TE | CR1_RE)) != (CR1_UE | CR1_TE | CR1_RE)) {
        /* Only (re)configure when needed: the app inherits the BL's setup and must not glitch a
         * byte that may still be in the shifter. */
        U_CR1(b) = 0;
        U_BRR(b) = brr;
        U_CR1(b) = CR1_TE | CR1_RE | CR1_UE;
    }
}

void hw_init(void) {
    clock_init();
    RCC_IOPENR |= 1u;                                /* GPIOA */
    RCC_APBENR1 |= RCC_APBENR1_USART2 | RCC_APBENR1_TIM3;
    RCC_APBENR2 |= RCC_APBENR2_USART1;
    (void)RCC_APBENR2;

    /* Pull-ups on the RX pins first: PA3 (VCP RX) and PA10 (USART1 RX; a floating line reads as
     * a stream of 0x00 while the ESP32 is not driving it). PA13/PA14 (SWD) are never touched. */
    uint32_t pu = GPIO_PUPDR(GPIOA_BASE);
    pu &= ~((3u << 6) | (3u << 20));
    pu |= (1u << 6) | (1u << 20);
    GPIO_PUPDR(GPIOA_BASE) = pu;
    /* AF1: PA2 USART2_TX, PA3 USART2_RX, PA9 USART1_TX, PA10 USART1_RX */
    uint32_t af = GPIO_AFRL(GPIOA_BASE);
    af &= ~((0xFu << 8) | (0xFu << 12));
    af |= (1u << 8) | (1u << 12);
    GPIO_AFRL(GPIOA_BASE) = af;
    af = GPIO_AFRH(GPIOA_BASE);
    af &= ~((0xFu << 4) | (0xFu << 8));
    af |= (1u << 4) | (1u << 8);
    GPIO_AFRH(GPIOA_BASE) = af;
    uint32_t m = GPIO_MODER(GPIOA_BASE);
    m &= ~((3u << 4) | (3u << 6) | (3u << (2u * LED_PIN)) | (3u << 18) | (3u << 20));
    m |= (2u << 4) | (2u << 6) | (1u << (2u * LED_PIN)) | (2u << 18) | (2u << 20);
    GPIO_MODER(GPIOA_BASE) = m;

    usart_init(USART1_BASE);
    usart_init(USART2_BASE);
    timebase_init();
}

/* ---- RX rings ---- */
#define RING 64u
static struct {
    uint8_t buf[RING];
    uint8_t head, tail;   /* head: next write, tail: next read */
    uint8_t lost;         /* a byte was dropped (ring full, overrun, framing/noise/parity error) */
} s_ring[NUM_UARTS];

static void ring_push(uint32_t u, uint8_t c) {
    uint8_t h = s_ring[u].head, n = (uint8_t)((h + 1u) & (RING - 1u));
    if (n == s_ring[u].tail) { s_ring[u].lost = 1; return; }
    s_ring[u].buf[h] = c;
    s_ring[u].head = n;
}

void uart_rx_service(void) {
    for (uint32_t u = 0; u < NUM_UARTS; u++) {
        uint32_t b = UBASE[u];
        uint32_t isr = U_ISR(b);
        uint32_t err = isr & (ISR_ORE | ISR_FE | ISR_NE | ISR_PE);
        if (err) U_ICR(b) = err;                     /* ORECF, NECF, FECF, PECF: same bit positions */
        if (isr & ISR_RXNE) {
            uint8_t c = (uint8_t)U_RDR(b);
            /* A damaged or lost byte becomes a NUL in the stream: the line it belongs to is bad. */
            ring_push(u, err ? 0u : c);
        } else if (err & ISR_ORE) {
            ring_push(u, 0u);
        }
    }
}

static int ring_pop(uint32_t u, char *c) {
    if (s_ring[u].tail == s_ring[u].head) {
        if (s_ring[u].lost) { s_ring[u].lost = 0; *c = '\0'; return 1; }
        return 0;
    }
    *c = (char)s_ring[u].buf[s_ring[u].tail];
    s_ring[u].tail = (uint8_t)((s_ring[u].tail + 1u) & (RING - 1u));
    return 1;
}

/* ---- Line assembly, one buffer per UART ---- */
static struct {
    char *buf;
    uint32_t len;
    uint8_t bad;
} s_line[NUM_UARTS];
static uint32_t s_max;
static uint32_t s_rr;     /* round robin: which UART is looked at first */

void line_init(char *buf_u1, char *buf_u2, uint32_t max) {
    s_line[0].buf = buf_u1;
    s_line[1].buf = buf_u2;
    for (uint32_t u = 0; u < NUM_UARTS; u++) { s_line[u].len = 0; s_line[u].bad = 0; }
    s_max = max;
}

char *line_buf(uint32_t u) { return s_line[u].buf; }

int line_poll(uint32_t *u_out) {
    uart_rx_service();
    for (uint32_t k = 0; k < NUM_UARTS; k++) {
        uint32_t u = (s_rr + k) & 1u;
        char c;
        while (ring_pop(u, &c)) {
            if (c == '\r' || c == '\n') {
                uint32_t len = s_line[u].len;
                int bad = s_line[u].bad;
                s_line[u].len = 0;
                s_line[u].bad = 0;
                if (!bad && len == 0) continue;         /* \r\n pairs, empty lines */
                s_rr = u ^ 1u;
                *u_out = u;
                if (bad) return -2;
                s_line[u].buf[len] = '\0';
                return (int)len;
            }
            if (c == '\0') s_line[u].bad = 1;
            else if (s_line[u].len < s_max) s_line[u].buf[s_line[u].len++] = c;
            else s_line[u].bad = 1;
        }
    }
    return 0;
}

/* ---- Output to both UARTs ---- */
void uart_putc(char c) {
    for (uint32_t u = 0; u < NUM_UARTS; u++) {
        uint32_t b = UBASE[u];
        while (!(U_ISR(b) & ISR_TXE)) uart_rx_service();
        U_TDR(b) = (uint32_t)(uint8_t)c;
    }
}

void uart_puts(const char *s) { while (*s) uart_putc(*s++); }
void uart_nl(void) { uart_putc('\r'); uart_putc('\n'); }
void uart_line(const char *s) { uart_puts(s); uart_nl(); }

void uart_flush(void) {
    for (uint32_t u = 0; u < NUM_UARTS; u++) {
        uint32_t t0 = time_us();
        /* bounded: a UART that is not clocked must not hang the reset path */
        while (!(U_ISR(UBASE[u]) & ISR_TC) && us_since(t0) < 20000u) { }
    }
}

static const char HEX[] = "0123456789ABCDEF";

void uart_hex32(uint32_t v) {
    for (int i = 28; i >= 0; i -= 4) uart_putc(HEX[(v >> i) & 0xFu]);
}

/* Decimal without a divide instruction (the M0+ has none): subtract powers of ten. */
void uart_u32(uint32_t v) {
    static const uint32_t P10[10] = {1000000000u, 100000000u, 10000000u, 1000000u, 100000u,
                                     10000u, 1000u, 100u, 10u, 1u};
    int started = 0;
    for (uint32_t i = 0; i < 10u; i++) {
        char d = '0';
        while (v >= P10[i]) { v -= P10[i]; d++; }
        if (d != '0' || started || i == 9u) { uart_putc(d); started = 1; }
    }
}

void uart_i32(int32_t v) {
    if (v < 0) { uart_putc('-'); uart_u32(0u - (uint32_t)v); }
    else uart_u32((uint32_t)v);
}

void uart_version(uint32_t v) {
    uart_u32((v >> 16) & 0xFFFFu); uart_putc('.');
    uart_u32((v >> 8) & 0xFFu);    uart_putc('.');
    uart_u32(v & 0xFFu);
}

/* RCC_CSR2 reset flags, in the SPEC 3 order. The C0 has no BOR flag (POR/PDR/BOR are PWRRSTF,
 * printed as POR); OBLRSTF (option byte loader) takes the last position. */
void uart_reset_cause(uint32_t csr) {
    static const struct { uint32_t bit; const char *name; } flags[] = {
        {CSR2_LPWRRSTF, "LPWR"}, {CSR2_WWDGRSTF, "WWDG"}, {CSR2_IWDGRSTF, "IWDG"},
        {CSR2_SFTRSTF, "SFT"},   {CSR2_PWRRSTF, "POR"},   {CSR2_PINRSTF, "PIN"},
        {CSR2_OBLRSTF, "OBL"},
    };
    int first = 1;
    for (unsigned i = 0; i < sizeof flags / sizeof flags[0]; i++) {
        if (csr & flags[i].bit) {
            if (!first) uart_putc(',');
            uart_puts(flags[i].name);
            first = 0;
        }
    }
    if (first) uart_puts("NONE");
}

/* ---- IWDG: LSI ~32 kHz / 32 = 1 kHz, reload 2000 -> ~2 s (SPEC 4.2) ---- */
void iwdg_start_2s(void) {
    IWDG_KR = 0xCCCCu;          /* start (also turns on the LSI) */
    IWDG_KR = 0x5555u;          /* unlock PR/RLR */
    IWDG_PR = 3u;               /* /32 */
    IWDG_RLR = 2000u;
    uint32_t t0 = time_us();
    while ((IWDG_SR & 7u) && us_since(t0) < 100000u) { }   /* PVU, RVU, WVU */
    IWDG_KR = 0xAAAAu;
}

/* Reloading a watchdog that was never started is harmless and does not start it. */
void iwdg_feed(void) { IWDG_KR = 0xAAAAu; }

void system_reset(void) {
    uart_flush();
    __asm volatile("dsb");
    SCB_AIRCR = AIRCR_SYSRESETREQ;
    __asm volatile("dsb");
    for (;;) { }
}

void led_set(int on) { GPIO_BSRR(GPIOA_BASE) = on ? (1u << LED_PIN) : (1u << (LED_PIN + 16u)); }
void led_toggle(void) { GPIO_ODR(GPIOA_BASE) ^= 1u << LED_PIN; }

/* ---- Strings ---- */
int str_eq(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

int starts_with(const char *s, const char *p) {
    while (*p) { if (*s++ != *p++) return 0; }
    return 1;
}

static void skip_one_space(const char **s) { if (**s == ' ') (*s)++; }

int parse_u32(const char **s, uint32_t *out) {
    const char *p = *s;
    uint32_t v = 0;
    if (*p < '0' || *p > '9') return 0;
    while (*p >= '0' && *p <= '9') {
        uint32_t d = (uint32_t)(*p - '0');
        if (v > 429496729u || (v == 429496729u && d > 5u)) return 0;   /* > 0xFFFFFFFF */
        v = (v << 3) + (v << 1) + d;
        p++;
    }
    if (*p != ' ' && *p != '\0') return 0;
    *s = p;
    skip_one_space(s);
    *out = v;
    return 1;
}

int parse_i32(const char **s, int32_t *out) {
    const char *p = *s;
    int neg = 0;
    if (*p == '-') { neg = 1; p++; }
    uint32_t v;
    if (!parse_u32(&p, &v) || v > 0x7FFFFFFFu) return 0;
    *s = p;
    *out = neg ? -(int32_t)v : (int32_t)v;
    return 1;
}

int parse_hex32(const char **s, uint32_t *out) {
    const char *p = *s;
    uint32_t v = 0;
    int n = 0;
    for (;; p++, n++) {
        char c = *p;
        uint32_t d;
        if (c >= '0' && c <= '9') d = (uint32_t)(c - '0');
        else if (c >= 'a' && c <= 'f') d = (uint32_t)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') d = (uint32_t)(c - 'A' + 10);
        else break;
        if (n >= 8) return 0;
        v = (v << 4) | d;
    }
    if (n == 0 || (*p != ' ' && *p != '\0')) return 0;
    *s = p;
    skip_one_space(s);
    *out = v;
    return 1;
}

const char *next_token(const char **s, uint32_t *len) {
    const char *p = *s;
    const char *start = p;
    while (*p && *p != ' ') p++;
    *len = (uint32_t)(p - start);
    *s = p;
    skip_one_space(s);
    return start;
}

void mem_copy(void *d, const void *s, uint32_t n) {
    uint8_t *dp = d; const uint8_t *sp = s;
    while (n--) *dp++ = *sp++;
}

int mem_eq(const void *a, const void *b, uint32_t n) {
    const uint8_t *x = a, *y = b;
    while (n--) if (*x++ != *y++) return 0;
    return 1;
}
