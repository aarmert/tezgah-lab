/*
 * stm32bench test firmware
 * Target: STM32F407G-DISC1 / STM32F411E-DISCO (LEDs on PD12-PD15)
 *
 * USART2: PA2 = TX (to the USB-TTL RX), PA3 = RX (to the USB-TTL TX), 115200 8N1
 * Clock: internal HSI 16 MHz (no PLL, does not depend on an external crystal)
 *
 * Commands (line ending \r\n or \n):
 *   PING            -> PONG
 *   VER             -> VER 1.2.0
 *   TICK            -> TICK <ms since boot>
 *   UID             -> UID <96-bit unique ID, hex>
 *   VDD             -> VDD <mV>      (VDDA measured with VREFINT + VREFINT_CAL)
 *   TEMP            -> TEMP <°C>     (internal temperature sensor, TS_CAL1/TS_CAL2, VDDA-corrected, 0.1 °C)
 *   LED <G|O|R|B> <ON|OFF> -> OK LED x ON/OFF   (touching G turns off the heartbeat)
 *   BLINK <1-20>    -> OK BLINK n   (green LED blinks at n Hz; for the camera test)
 *   CRASH           -> OK CRASH, then a deliberate HardFault (for testing)
 *   RESET           -> OK RESET, then a software reset
 *
 * Blue user button (PA0): each press toggles the blue LED (PD15)
 * and prints BTN <ON|OFF> <press count> on the serial port.
 *
 * At boot:  BOOT stm32bench-fw v1.2.0 reset=<cause>
 * On crash: HARDFAULT pc=0x........ lr=0x........ cfsr=0x........ hfsr=0x........
 */
#include <stdint.h>

#define FW_VERSION "1.2.0"

/* ---- Register addresses (STM32F4) ---- */
#define REG(addr) (*(volatile uint32_t *)(addr))

#define RCC_BASE     0x40023800u
#define RCC_AHB1ENR  REG(RCC_BASE + 0x30)
#define RCC_APB1ENR  REG(RCC_BASE + 0x40)
#define RCC_APB2ENR  REG(RCC_BASE + 0x44)
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

/* ADC1 and common ADC registers (RM0090, CMSIS stm32f407xx.h) */
#define ADC1_BASE    0x40012000u
#define ADC_SR       REG(ADC1_BASE + 0x00)
#define ADC_CR1      REG(ADC1_BASE + 0x04)
#define ADC_CR2      REG(ADC1_BASE + 0x08)
#define ADC_SMPR1    REG(ADC1_BASE + 0x0C)
#define ADC_SQR1     REG(ADC1_BASE + 0x2C)
#define ADC_SQR3     REG(ADC1_BASE + 0x34)
#define ADC_DR       REG(ADC1_BASE + 0x4C)
#define ADC_CCR      REG(0x40012304u)
#define ADC_SR_EOC   (1u << 1)
#define ADC_CR2_ADON (1u << 0)
#define ADC_CR2_SWSTART (1u << 30)
#define ADC_CCR_TSVREFE (1u << 23)

/* STM32F40x/41x: temperature sensor on ADC1_IN16, VREFINT on ADC1_IN17
 * (on the F411/F42x the sensor is on IN18, shared with VBAT; this code is for the F407). */
#define ADC_CH_TEMP     16u
#define ADC_CH_VREFINT  17u
#define ADC_SAMPLES     16u

/* Factory calibration (system memory, 16 bit, measured at 30 °C / 110 °C, VDDA = 3.3 V) */
#define VREFINT_CAL  (*(volatile uint16_t *)0x1FFF7A2Au)
#define TS_CAL1      (*(volatile uint16_t *)0x1FFF7A2Cu)
#define TS_CAL2      (*(volatile uint16_t *)0x1FFF7A2Eu)
#define CAL_VDDA_MV  3300u

#define SYST_CSR     REG(0xE000E010u)
#define SYST_RVR     REG(0xE000E014u)
#define SYST_CVR     REG(0xE000E018u)

#define SCB_AIRCR    REG(0xE000ED0Cu)
#define SCB_CFSR     REG(0xE000ED28u)
#define SCB_HFSR     REG(0xE000ED2Cu)

#define UID_BASE     0x1FFF7A10u

#define HSI_HZ       16000000u
#define BAUD         115200u

/* LEDs: G=PD12, O=PD13, R=PD14, B=PD15 */
#define LED_PIN(i)   (12u + (i))
#define LED_BLUE     3u

/* User button B1: PA0, reads 1 when pressed (the board has a pull-down) */
#define BTN_PIN      0u
#define BTN_DEBOUNCE_MS 20u

static volatile uint32_t g_ms;
static uint32_t g_blink_half_ms = 250;  /* 2 Hz */
static int g_heartbeat = 1;

/* ---- Hardware setup ---- */
static void clocks_init(void) {
    RCC_AHB1ENR |= (1u << 0) | (1u << 3);   /* GPIOA, GPIOD */
    RCC_APB1ENR |= (1u << 17);              /* USART2 */
    RCC_APB2ENR |= (1u << 8);               /* ADC1 */
    (void)RCC_APB2ENR;
}

static void gpio_init(void) {
    /* PD12-PD15 output */
    uint32_t m = GPIO_MODER(GPIOD_BASE);
    m &= ~(0xFFu << 24);
    m |= (0x55u << 24);
    GPIO_MODER(GPIOD_BASE) = m;

    /* PA2, PA3 alternate function AF7 (USART2) */
    m = GPIO_MODER(GPIOA_BASE);
    m &= ~((3u << 4) | (3u << 6));
    m |= (2u << 4) | (2u << 6);
    GPIO_MODER(GPIOA_BASE) = m;

    uint32_t af = GPIO_AFRL(GPIOA_BASE);
    af &= ~((0xFu << 8) | (0xFu << 12));
    af |= (7u << 8) | (7u << 12);
    GPIO_AFRL(GPIOA_BASE) = af;

    /* PA3 (RX) pull-up: no noise reads while the USB-TTL is unplugged */
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
    SYST_CSR = 7u;  /* CPU clock, interrupt, enable */
}

void SysTick_Handler(void) { g_ms++; }

/* ADC1: PCLK2 = 16 MHz, ADCPRE = /2 -> 8 MHz ADC clock (36 MHz max).
 * Channels 16 and 17 sample for 480 cycles = 60 µs (datasheet: at least 10 µs on internal channels). */
static void adc_init(void) {
    ADC_CCR = (ADC_CCR & ~(3u << 16)) | ADC_CCR_TSVREFE;   /* ADCPRE=/2, sensor and VREFINT on */
    ADC_CR1 = 0;                                          /* 12 bit, no scan */
    ADC_SMPR1 = (ADC_SMPR1 & ~((7u << 18) | (7u << 21))) | (7u << 18) | (7u << 21);
    ADC_SQR1 = 0;                                         /* 1 conversion in the sequence */
    ADC_CR2 = ADC_CR2_ADON;
    /* tSTART (sensor/VREFINT) and ADC tSTAB are at most 10 µs: wait 2 ms */
    uint32_t t0 = g_ms;
    while ((g_ms - t0) < 2u) { }
}

static uint32_t adc_read(uint32_t ch) {
    ADC_SQR3 = ch;
    (void)ADC_DR;                         /* clear a stale EOC */
    ADC_CR2 |= ADC_CR2_SWSTART;
    while (!(ADC_SR & ADC_SR_EOC)) { }
    return ADC_DR & 0xFFFu;               /* reading DR clears EOC */
}

static uint32_t adc_sum(uint32_t ch) {
    uint32_t sum = 0;
    for (uint32_t i = 0; i < ADC_SAMPLES; i++) sum += adc_read(ch);
    return sum;
}

/* Signed integer division, rounds to nearest (den > 0) */
static int64_t div_round(int64_t num, int64_t den) {
    return (num >= 0) ? (num + den / 2) / den : -((-num + den / 2) / den);
}

/* VDDA = 3300 mV * VREFINT_CAL / VREFINT_read (average) */
static uint32_t vdda_mv(void) {
    uint32_t vref_sum = adc_sum(ADC_CH_VREFINT);
    if (!vref_sum) return 0;
    return (uint32_t)div_round((int64_t)CAL_VDDA_MV * VREFINT_CAL * ADC_SAMPLES, vref_sum);
}

/* Temperature in units of 0.1 °C. The raw sensor value is scaled to the 3.3 V of the calibration:
 *   ts_33 = ts * VDDA / 3300 = ts * VREFINT_CAL / vref
 *   T = 30 + (110 - 30) * (ts_33 - TS_CAL1) / (TS_CAL2 - TS_CAL1)
 * Averages are kept as sums; N cancels between numerator and denominator. */
static int32_t temp_x10(void) {
    uint32_t vref_sum = adc_sum(ADC_CH_VREFINT);
    uint32_t ts_sum = adc_sum(ADC_CH_TEMP);
    int32_t span = (int32_t)TS_CAL2 - (int32_t)TS_CAL1;
    if (!vref_sum || span <= 0) return INT32_MIN;
    int64_t num = 800 * ((int64_t)ts_sum * VREFINT_CAL - (int64_t)TS_CAL1 * vref_sum);
    return 300 + (int32_t)div_round(num, (int64_t)span * vref_sum);
}

/* ---- UART helpers ---- */
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

/* Print a signed value in units of 0.1 as "-12.3" */
static void uart_fixed1(int32_t v) {
    uint32_t u = (uint32_t)v;
    if (v < 0) { uart_putc('-'); u = 0u - u; }
    uart_u32(u / 10u); uart_putc('.'); uart_putc((char)('0' + u % 10u));
}

/* ---- Small string helpers (no libc) ---- */
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

/* ---- Reset cause ---- */
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
    RCC_CSR |= (1u << 24);  /* RMVF: clear the flags */
}

/* ---- HardFault: take PC/LR from the stack, report them, then stop ---- */
__attribute__((used)) void hardfault_report(uint32_t *frame) {
    uart_nl();
    uart_puts("HARDFAULT pc=0x"); uart_hex32(frame[6]);
    uart_puts(" lr=0x");          uart_hex32(frame[5]);
    uart_puts(" cfsr=0x");        uart_hex32(SCB_CFSR);
    uart_puts(" hfsr=0x");        uart_hex32(SCB_HFSR);
    uart_nl();
    for (uint32_t i = 0; i < 4; i++) led_set(i, i == 2);  /* red only */
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

/* ---- Command handling ---- */
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
    } else if (str_eq(line, "VDD")) {
        uint32_t mv = vdda_mv();
        if (mv) { uart_puts("VDD "); uart_u32(mv); }
        else    { uart_puts("ERR adc"); }
    } else if (str_eq(line, "TEMP")) {
        int32_t t = temp_x10();
        if (t != INT32_MIN) { uart_puts("TEMP "); uart_fixed1(t); }
        else                { uart_puts("ERR calibration"); }
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
        __asm volatile("udf #0");  /* undefined instruction -> HardFault */
        return;
    } else if (str_eq(line, "RESET")) {
        uart_puts("OK RESET");
        uart_nl();
        while (!(USART_SR & (1u << 6))) { }  /* TC: let the last byte go out */
        SCB_AIRCR = 0x05FA0004u;             /* SYSRESETREQ */
        for (;;) { }
    } else if (line[0] == '\0') {
        return;
    } else {
        uart_puts("ERR unknown command");
    }
    uart_nl();
}

/* ---- Button: sample every 1 ms, accept once stable for BTN_DEBOUNCE_MS ---- */
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
    if (!stable) return;  /* only on the press edge */

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
    adc_init();

    uart_nl();
    uart_puts("BOOT stm32bench-fw v" FW_VERSION " reset=");
    print_reset_cause();
    uart_nl();

    char line[64];
    uint32_t len = 0;
    int overflow = 0;
    uint32_t last_toggle = 0;

    for (;;) {
        /* Heartbeat: the green LED blinks while the main loop runs. It stops if the loop hangs. */
        if (g_heartbeat && (g_ms - last_toggle) >= g_blink_half_ms) {
            last_toggle = g_ms;
            GPIO_ODR(GPIOD_BASE) ^= (1u << LED_PIN(0));
        }

        button_poll();

        uint32_t sr = USART_SR;
        if (sr & SR_ORE) { (void)USART_DR; }      /* clear the overrun flag */
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
