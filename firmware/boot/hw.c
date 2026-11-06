/* Clocks, GPIO, USART2, DWT timing, IWDG, reset and small string helpers (no libc). */
#include "common.h"

void hw_init(void) {
    RCC_AHB1ENR |= (1u << 0) | (1u << 3);   /* GPIOA, GPIOD */
    RCC_APB1ENR |= (1u << 17) | (1u << 0);  /* USART2, TIM2 */
    (void)RCC_APB1ENR;

    /* PD12-PD15 outputs (LEDs) */
    uint32_t m = GPIO_MODER(GPIOD_BASE);
    m &= ~(0xFFu << 24);
    m |= (0x55u << 24);
    GPIO_MODER(GPIOD_BASE) = m;

    /* PA2/PA3 AF7 (USART2). PA13/PA14 (SWD) are never touched. */
    m = GPIO_MODER(GPIOA_BASE);
    m &= ~((3u << 4) | (3u << 6));
    m |= (2u << 4) | (2u << 6);
    GPIO_MODER(GPIOA_BASE) = m;
    uint32_t af = GPIO_AFRL(GPIOA_BASE);
    af &= ~((0xFu << 8) | (0xFu << 12));
    af |= (7u << 8) | (7u << 12);
    GPIO_AFRL(GPIOA_BASE) = af;
    uint32_t pu = GPIO_PUPDR(GPIOA_BASE);
    pu &= ~(3u << 6);
    pu |= (1u << 6);
    GPIO_PUPDR(GPIOA_BASE) = pu;

    uint32_t brr = (HSI_HZ + BAUD / 2u) / BAUD;
    if (USART_BRR != brr || (USART_CR1 & 0x200Cu) != 0x200Cu) {
        /* Only (re)configure when needed: the app inherits the BL's USART setup and must not
         * glitch a byte that may still be in the shifter. */
        USART_CR1 = 0;
        USART_BRR = brr;
        USART_CR1 = (1u << 13) | (1u << 3) | (1u << 2);  /* UE, TE, RE */
    }

    /* DWT cycle counter for timing (works without SysTick, also while the flash is busy) */
    DEMCR |= (1u << 24);       /* TRCENA */
    DWT_CTRL |= 1u;            /* CYCCNTENA */

    /* FLASH_ACR is left at its reset value: 0 wait states at 16 MHz, caches and prefetch off,
     * so reads after erase/program never see stale cache lines. */
}

/* ---- UART ---- */
void uart_putc(char c) {
    while (!(USART_SR & SR_TXE)) { }
    USART_DR = (uint32_t)(uint8_t)c;
}

void uart_puts(const char *s) { while (*s) uart_putc(*s++); }
void uart_nl(void) { uart_putc('\r'); uart_putc('\n'); }
void uart_line(const char *s) { uart_puts(s); uart_nl(); }
void uart_flush(void) { while (!(USART_SR & SR_TC)) { } }

static const char HEX[] = "0123456789ABCDEF";

void uart_hex32(uint32_t v) {
    for (int i = 28; i >= 0; i -= 4) uart_putc(HEX[(v >> i) & 0xFu]);
}

void uart_hex8(uint8_t v) { uart_putc(HEX[v >> 4]); uart_putc(HEX[v & 0xFu]); }

void uart_u32(uint32_t v) {
    char buf[11];
    int i = 0;
    do { buf[i++] = (char)('0' + (v % 10u)); v /= 10u; } while (v && i < 10);
    while (i) uart_putc(buf[--i]);
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

void uart_reset_cause(uint32_t csr) {
    static const struct { uint32_t bit; const char *name; } flags[] = {
        {1u << 31, "LPWR"}, {1u << 30, "WWDG"}, {1u << 29, "IWDG"}, {1u << 28, "SFT"},
        {1u << 27, "POR"},  {1u << 26, "PIN"},  {1u << 25, "BOR"},
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

int uart_poll_char(char *c) {
    uint32_t sr = USART_SR;
    if (sr & SR_ORE) (void)USART_DR;          /* clear overrun */
    if (!(sr & SR_RXNE)) return 0;
    *c = (char)(USART_DR & 0xFFu);
    return 1;
}

int uart_getline(char *buf, uint32_t max, uint32_t timeout_ms) {
    uint32_t len = 0;
    int overflow = 0;
    uint32_t t0 = dwt_now();
    for (;;) {
        char c;
        if (!uart_poll_char(&c)) {
            if (timeout_ms && dwt_us_since(t0) >= timeout_ms * 1000u) return -1;
            continue;
        }
        t0 = dwt_now();
        if (c == '\r' || c == '\n') {
            if (overflow) return -2;
            if (len == 0) continue;           /* \r\n pairs, empty lines */
            buf[len] = '\0';
            return (int)len;
        }
        if (len < max) buf[len++] = c;
        else overflow = 1;
    }
}

/* ---- Time ---- */
uint32_t dwt_now(void) { return DWT_CYCCNT; }
uint32_t dwt_us_since(uint32_t t0) { return (DWT_CYCCNT - t0) / CYC_PER_US; }

void delay_ms(uint32_t ms) {
    uint32_t t0 = dwt_now();
    while (dwt_us_since(t0) < ms * 1000u) { }
}

/* ---- IWDG: LSI ~32 kHz / 32 = 1 kHz, reload 2000 -> ~2 s (SPEC 4.2) ---- */
void iwdg_start_2s(void) {
    IWDG_KR = 0xCCCCu;          /* start (also turns on the LSI) */
    IWDG_KR = 0x5555u;          /* unlock PR/RLR */
    IWDG_PR = 3u;               /* /32 */
    IWDG_RLR = 2000u;
    uint32_t t0 = dwt_now();
    while ((IWDG_SR & 3u) && dwt_us_since(t0) < 100000u) { }
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

void led_set(uint32_t idx, int on) {
    GPIO_BSRR(GPIOD_BASE) = on ? (1u << (12u + idx)) : (1u << (12u + idx + 16u));
}

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
        if (v > (0xFFFFFFFFu - d) / 10u) return 0;
        v = v * 10u + d;
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

/* Returns the start of the next space-separated token and its length; advances *s past it. */
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
