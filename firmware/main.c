/*
 * stm32bench test firmware
 * Hedef: STM32F407G-DISC1 / STM32F411E-DISCO (LED'ler PD12-PD15)
 *
 * USART2: PA2 = TX (USB-TTL'in RX'ine), PA3 = RX (USB-TTL'in TX'ine), 115200 8N1
 * Saat: dahili HSI 16 MHz (PLL yok, harici kristale bağımlı değil)
 *
 * Komutlar (satır sonu \r\n veya \n):
 *   PING            -> PONG
 *   VER             -> VER 1.0.0
 *   TICK            -> TICK <açılıştan beri ms>
 *   UID             -> UID <96 bit benzersiz kimlik, hex>
 *   LED <G|O|R|B> <ON|OFF> -> OK LED x ON/OFF   (G'ye dokunmak heartbeat'i kapatır)
 *   BLINK <1-20>    -> OK BLINK n   (yeşil LED n Hz yanıp söner; kamera testi için)
 *   CRASH           -> OK CRASH, ardından bilerek HardFault (test için)
 *   RESET           -> OK RESET, ardından yazılım reseti
 *
 * Mavi kullanıcı butonu (PA0): her basışta mavi LED (PD15) açılır/kapanır,
 * seri porta BTN <ON|OFF> <basış sayısı> basılır.
 *
 * Açılışta: BOOT stm32bench-fw v1.0.0 reset=<sebep>
 * Çökünce:  HARDFAULT pc=0x........ lr=0x........ cfsr=0x........ hfsr=0x........
 */
#include <stdint.h>

#define FW_VERSION "1.1.0"

/* ---- Register adresleri (STM32F4) ---- */
#define REG(addr) (*(volatile uint32_t *)(addr))

#define RCC_BASE     0x40023800u
#define RCC_AHB1ENR  REG(RCC_BASE + 0x30)
#define RCC_APB1ENR  REG(RCC_BASE + 0x40)
#define RCC_CSR      REG(RCC_BASE + 0x74)

#define GPIOA_BASE   0x40020000u
#define GPIOD_BASE   0x40020C00u
#define GPIO_MODER(b)   REG((b) + 0x00)
#define GPIO_PUPDR(b)   REG((b) + 0x0C)
#define GPIO_IDR(b)     REG((b) + 0x10)
#define GPIO_ODR(b)     REG((b) + 0x14)
#define GPIO_BSRR(b)    REG((b) + 0x18)
#define GPIO_AFRL(b)    REG((b) + 0x20)

#define USART2_BASE  0x40004400u
#define USART_SR     REG(USART2_BASE + 0x00)
#define USART_DR     REG(USART2_BASE + 0x04)
#define USART_BRR    REG(USART2_BASE + 0x08)
#define USART_CR1    REG(USART2_BASE + 0x0C)
#define SR_RXNE      (1u << 5)
#define SR_TXE       (1u << 7)
#define SR_ORE       (1u << 3)

#define SYST_CSR     REG(0xE000E010u)
#define SYST_RVR     REG(0xE000E014u)
#define SYST_CVR     REG(0xE000E018u)

#define SCB_AIRCR    REG(0xE000ED0Cu)
#define SCB_CFSR     REG(0xE000ED28u)
#define SCB_HFSR     REG(0xE000ED2Cu)

#define UID_BASE     0x1FFF7A10u

#define HSI_HZ       16000000u
#define BAUD         115200u

/* LED'ler: G=PD12, O=PD13, R=PD14, B=PD15 */
#define LED_PIN(i)   (12u + (i))
#define LED_BLUE     3u

/* Kullanıcı butonu B1: PA0, basınca 1 (kartta pull-down var) */
#define BTN_PIN      0u
#define BTN_DEBOUNCE_MS 20u

static volatile uint32_t g_ms;
static uint32_t g_blink_half_ms = 250;  /* 2 Hz */
static int g_heartbeat = 1;

/* ---- Donanım kurulumu ---- */
static void clocks_init(void) {
    RCC_AHB1ENR |= (1u << 0) | (1u << 3);   /* GPIOA, GPIOD */
    RCC_APB1ENR |= (1u << 17);              /* USART2 */
    (void)RCC_APB1ENR;
}

static void gpio_init(void) {
    /* PD12-PD15 çıkış */
    uint32_t m = GPIO_MODER(GPIOD_BASE);
    m &= ~(0xFFu << 24);
    m |= (0x55u << 24);
    GPIO_MODER(GPIOD_BASE) = m;

    /* PA2, PA3 alternatif fonksiyon AF7 (USART2) */
    m = GPIO_MODER(GPIOA_BASE);
    m &= ~((3u << 4) | (3u << 6));
    m |= (2u << 4) | (2u << 6);
    GPIO_MODER(GPIOA_BASE) = m;

    uint32_t af = GPIO_AFRL(GPIOA_BASE);
    af &= ~((0xFu << 8) | (0xFu << 12));
    af |= (7u << 8) | (7u << 12);
    GPIO_AFRL(GPIOA_BASE) = af;

    /* PA3 (RX) pull-up: USB-TTL takılı değilken parazit okumasın */
    uint32_t pu = GPIO_PUPDR(GPIOA_BASE);
    pu &= ~(3u << 6);
    pu |= (1u << 6);
    GPIO_PUPDR(GPIOA_BASE) = pu;
}

static void uart_init(void) {
    USART_BRR = (HSI_HZ + BAUD / 2u) / BAUD;
    USART_CR1 = (1u << 13) | (1u << 3) | (1u << 2);  /* UE, TE, RE */
}

static void systick_init(void) {
    SYST_RVR = (HSI_HZ / 1000u) - 1u;
    SYST_CVR = 0;
    SYST_CSR = 7u;  /* işlemci saati, kesme, aç */
}

void SysTick_Handler(void) { g_ms++; }

/* ---- UART yardımcıları ---- */
static void uart_putc(char c) {
    while (!(USART_SR & SR_TXE)) { }
    USART_DR = (uint32_t)(uint8_t)c;
}

static void uart_puts(const char *s) {
    while (*s) uart_putc(*s++);
}

static void uart_hex32(uint32_t v) {
    static const char hex[] = "0123456789ABCDEF";
    for (int i = 28; i >= 0; i -= 4) uart_putc(hex[(v >> i) & 0xFu]);
}

static void uart_u32(uint32_t v) {
    char buf[11];
    int i = 0;
    do { buf[i++] = (char)('0' + (v % 10u)); v /= 10u; } while (v && i < 10);
    while (i) uart_putc(buf[--i]);
}

static void uart_nl(void) { uart_puts("\r\n"); }

/* ---- Küçük string yardımcıları (libc yok) ---- */
static char upper(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }

static int str_eq(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

static int starts_with(const char *s, const char *p) {
    while (*p) { if (*s++ != *p++) return 0; }
    return 1;
}

static int parse_u32(const char *s, uint32_t *out) {
    uint32_t v = 0;
    if (*s < '0' || *s > '9') return 0;
    while (*s >= '0' && *s <= '9') { v = v * 10u + (uint32_t)(*s - '0'); s++; }
    if (*s) return 0;
    *out = v;
    return 1;
}

/* ---- LED ---- */
static void led_set(uint32_t idx, int on) {
    GPIO_BSRR(GPIOD_BASE) = on ? (1u << LED_PIN(idx)) : (1u << (LED_PIN(idx) + 16u));
}

static int led_index(char c) {
    switch (c) {
    case 'G': return 0;
    case 'O': return 1;
    case 'R': return 2;
    case 'B': return 3;
    default:  return -1;
    }
}

/* ---- Reset sebebi ---- */
static void print_reset_cause(void) {
    uint32_t csr = RCC_CSR;
    int first = 1;
    static const struct { uint32_t bit; const char *name; } flags[] = {
        {1u << 31, "LPWR"}, {1u << 30, "WWDG"}, {1u << 29, "IWDG"}, {1u << 28, "SFT"},
        {1u << 27, "POR"},  {1u << 26, "PIN"},  {1u << 25, "BOR"},
    };
    for (unsigned i = 0; i < sizeof flags / sizeof flags[0]; i++) {
        if (csr & flags[i].bit) {
            if (!first) uart_putc(',');
            uart_puts(flags[i].name);
            first = 0;
        }
    }
    if (first) uart_puts("NONE");
    RCC_CSR |= (1u << 24);  /* RMVF: bayrakları temizle */
}

/* ---- HardFault: yığından PC/LR'yi alıp raporla, sonra dur ---- */
__attribute__((used)) void hardfault_report(uint32_t *frame) {
    uart_nl();
    uart_puts("HARDFAULT pc=0x"); uart_hex32(frame[6]);
    uart_puts(" lr=0x");          uart_hex32(frame[5]);
    uart_puts(" cfsr=0x");        uart_hex32(SCB_CFSR);
    uart_puts(" hfsr=0x");        uart_hex32(SCB_HFSR);
    uart_nl();
    for (uint32_t i = 0; i < 4; i++) led_set(i, i == 2);  /* sadece kırmızı */
    for (;;) { }
}

__attribute__((naked)) void HardFault_Handler(void) {
    __asm volatile(
        "tst lr, #4      \n"
        "ite eq          \n"
        "mrseq r0, msp   \n"
        "mrsne r0, psp   \n"
        "b hardfault_report \n");
}

/* ---- Komut işleme ---- */
static void handle_command(char *line) {
    for (char *p = line; *p; p++) *p = upper(*p);

    if (str_eq(line, "PING")) {
        uart_puts("PONG");
    } else if (str_eq(line, "VER")) {
        uart_puts("VER " FW_VERSION);
    } else if (str_eq(line, "TICK")) {
        uart_puts("TICK "); uart_u32(g_ms);
    } else if (str_eq(line, "UID")) {
        uart_puts("UID ");
        for (uint32_t i = 0; i < 3; i++) uart_hex32(REG(UID_BASE + 4u * i));
    } else if (starts_with(line, "LED ") && line[4] && line[5] == ' ') {
        int idx = led_index(line[4]);
        const char *state = &line[6];
        int on = str_eq(state, "ON");
        if (idx < 0 || (!on && !str_eq(state, "OFF"))) {
            uart_puts("ERR usage: LED <G|O|R|B> <ON|OFF>");
        } else {
            if (idx == 0) g_heartbeat = 0;
            led_set((uint32_t)idx, on);
            uart_puts("OK LED "); uart_putc(line[4]); uart_puts(on ? " ON" : " OFF");
        }
    } else if (starts_with(line, "BLINK ")) {
        uint32_t hz;
        if (parse_u32(&line[6], &hz) && hz >= 1u && hz <= 20u) {
            g_blink_half_ms = 500u / hz;
            g_heartbeat = 1;
            uart_puts("OK BLINK "); uart_u32(hz);
        } else {
            uart_puts("ERR range 1-20");
        }
    } else if (str_eq(line, "CRASH")) {
        uart_puts("OK CRASH");
        uart_nl();
        __asm volatile("udf #0");  /* tanımsız komut -> HardFault */
        return;
    } else if (str_eq(line, "RESET")) {
        uart_puts("OK RESET");
        uart_nl();
        while (!(USART_SR & (1u << 6))) { }  /* TC: son bayt gitsin */
        SCB_AIRCR = 0x05FA0004u;             /* SYSRESETREQ */
        for (;;) { }
    } else if (line[0] == '\0') {
        return;
    } else {
        uart_puts("ERR unknown command");
    }
    uart_nl();
}

/* ---- Buton: 1 ms'de bir örnekle, BTN_DEBOUNCE_MS boyunca sabit kalınca kabul et ---- */
static void button_poll(void) {
    static uint32_t last_sample, stable_since, pressed_count;
    static int raw_last, stable, blue_on;

    if (g_ms == last_sample) return;
    last_sample = g_ms;

    int raw = (GPIO_IDR(GPIOA_BASE) >> BTN_PIN) & 1;
    if (raw != raw_last) {
        raw_last = raw;
        stable_since = g_ms;
        return;
    }
    if (raw == stable || (g_ms - stable_since) < BTN_DEBOUNCE_MS) return;

    stable = raw;
    if (!stable) return;  /* sadece basma kenarında */

    blue_on = !blue_on;
    pressed_count++;
    led_set(LED_BLUE, blue_on);
    uart_puts(blue_on ? "BTN ON " : "BTN OFF "); uart_u32(pressed_count); uart_nl();
}

int main(void) {
    clocks_init();
    gpio_init();
    uart_init();
    systick_init();

    uart_nl();
    uart_puts("BOOT stm32bench-fw v" FW_VERSION " reset=");
    print_reset_cause();
    uart_nl();

    char line[64];
    uint32_t len = 0;
    int overflow = 0;
    uint32_t last_toggle = 0;

    for (;;) {
        /* Heartbeat: ana döngü dönüyorsa yeşil LED yanıp söner. Döngü takılırsa durur. */
        if (g_heartbeat && (g_ms - last_toggle) >= g_blink_half_ms) {
            last_toggle = g_ms;
            GPIO_ODR(GPIOD_BASE) ^= (1u << LED_PIN(0));
        }

        button_poll();

        uint32_t sr = USART_SR;
        if (sr & SR_ORE) { (void)USART_DR; }      /* taşma bayrağını temizle */
        if (!(sr & SR_RXNE)) continue;

        char c = (char)(USART_DR & 0xFFu);
        if (c == '\r' || c == '\n') {
            if (overflow) {
                uart_puts("ERR line too long"); uart_nl();
            } else if (len) {
                line[len] = '\0';
                handle_command(line);
            }
            len = 0;
            overflow = 0;
        } else if (len < sizeof line - 1u) {
            line[len++] = c;
        } else {
            overflow = 1;
        }
    }
}
