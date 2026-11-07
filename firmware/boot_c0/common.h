/*
 * Shared declarations for the C0 bootloaders and apps (register level, no HAL).
 * STM32C031C6 (Cortex-M0+, ARMv6-M). Register addresses: RM0490. Items marked (verify) in
 * docs/stress/SPEC-C031C6.md are listed in README.md with what was and was not confirmed.
 */
#ifndef COMMON_H
#define COMMON_H
#include <stdint.h>
#include <stddef.h>
#include "layout.h"

#define REG(addr) (*(volatile uint32_t *)(addr))

/* Code that must run while the flash is busy (SPEC 5). long_call: it lives in RAM, out of reach
 * of a Thumb BL from flash. */
#define RAMFUNC __attribute__((section(".ramfunc"), noinline, long_call))
/* Interrupt handlers in RAM (referenced by address from the vector table, never called) */
#define RAMISR  __attribute__((section(".ramfunc"), noinline, used))

/* ---- RCC (0x40021000) ---- */
#define RCC_BASE     0x40021000u
#define RCC_CR       REG(RCC_BASE + 0x00)
#define RCC_CFGR     REG(RCC_BASE + 0x08)
#define RCC_IOPENR   REG(RCC_BASE + 0x34)
#define RCC_APBENR1  REG(RCC_BASE + 0x3C)
#define RCC_APBENR2  REG(RCC_BASE + 0x40)
#define RCC_CSR2     REG(RCC_BASE + 0x60)
#define RCC_CR_HSIRDY      (1u << 10)
#define RCC_CR_HSIDIV_MSK  (7u << 11)
#define RCC_APBENR1_TIM3   (1u << 1)
#define RCC_APBENR1_USART2 (1u << 17)
#define RCC_APBENR2_USART1 (1u << 14)
#define RCC_APBENR2_TIM14  (1u << 15)
#define CSR2_RMVF    (1u << 23)
#define CSR2_OBLRSTF (1u << 25)
#define CSR2_PINRSTF (1u << 26)
#define CSR2_PWRRSTF (1u << 27)
#define CSR2_SFTRSTF (1u << 28)
#define CSR2_IWDGRSTF (1u << 29)
#define CSR2_WWDGRSTF (1u << 30)
#define CSR2_LPWRRSTF (1u << 31)

/* ---- GPIO (IOPORT bus) ---- */
#define GPIOA_BASE   0x50000000u
#define GPIO_MODER(b)   REG((b) + 0x00)
#define GPIO_PUPDR(b)   REG((b) + 0x0C)
#define GPIO_ODR(b)     REG((b) + 0x14)
#define GPIO_BSRR(b)    REG((b) + 0x18)
#define GPIO_AFRL(b)    REG((b) + 0x20)
#define GPIO_AFRH(b)    REG((b) + 0x24)
#define LED_PIN      5u            /* LD4 = PA5 (NUCLEO-C031C6) */

/* ---- USART1 (update channel, PA9/PA10) and USART2 (ST-LINK VCP witness, PA2/PA3) ---- */
#define USART1_BASE  0x40013800u
#define USART2_BASE  0x40004400u
#define U_CR1(b)     REG((b) + 0x00)
#define U_BRR(b)     REG((b) + 0x0C)
#define U_ISR(b)     REG((b) + 0x1C)
#define U_ICR(b)     REG((b) + 0x20)
#define U_RDR(b)     REG((b) + 0x24)
#define U_TDR(b)     REG((b) + 0x28)
#define ISR_PE       (1u << 0)
#define ISR_FE       (1u << 1)
#define ISR_NE       (1u << 2)
#define ISR_ORE      (1u << 3)
#define ISR_RXNE     (1u << 5)
#define ISR_TC       (1u << 6)
#define ISR_TXE      (1u << 7)
#define CR1_UE       (1u << 0)
#define CR1_RE       (1u << 2)
#define CR1_TE       (1u << 3)
#define NUM_UARTS    2u

/* ---- Flash interface (0x40022000) ---- */
#define FLASH_IF     0x40022000u
#define FLASH_ACR    REG(FLASH_IF + 0x00)
#define FLASH_KEYR   REG(FLASH_IF + 0x08)
#define FLASH_SR     REG(FLASH_IF + 0x10)
#define FLASH_CR     REG(FLASH_IF + 0x14)
#define FLASH_OPTR   REG(FLASH_IF + 0x20)   /* read only here: option bytes are never written */
#define ACR_LATENCY_MSK (7u << 0)
#define ACR_ICEN     (1u << 9)
#define ACR_ICRST    (1u << 11)

/* ---- General purpose timers (16 bit). TIM3: fault timer. TIM14: free-running 1 MHz clock. ---- */
#define TIM3_BASE    0x40000400u
#define TIM14_BASE   0x40002000u
#define TIM_CR1(b)   REG((b) + 0x00)
#define TIM_DIER(b)  REG((b) + 0x0C)
#define TIM_SR(b)    REG((b) + 0x10)
#define TIM_EGR(b)   REG((b) + 0x14)
#define TIM_CCMR1(b) REG((b) + 0x18)
#define TIM_CCER(b)  REG((b) + 0x20)
#define TIM_CNT(b)   REG((b) + 0x24)
#define TIM_PSC(b)   REG((b) + 0x28)
#define TIM_ARR(b)   REG((b) + 0x2C)
#define TIM_CCR1(b)  REG((b) + 0x34)
#define TIM_CR1_CEN  (1u << 0)
#define TIM_CR1_URS  (1u << 2)
#define TIM_SR_UIF   (1u << 0)
#define TIM_SR_CC1IF (1u << 1)
#define TIM3_IRQN    16u
#define TIM14_IRQN   19u

/* ---- IWDG ---- */
#define IWDG_BASE    0x40003000u
#define IWDG_KR      REG(IWDG_BASE + 0x00)
#define IWDG_PR      REG(IWDG_BASE + 0x04)
#define IWDG_RLR     REG(IWDG_BASE + 0x08)
#define IWDG_SR      REG(IWDG_BASE + 0x0C)

/* ---- Core (ARMv6-M: no CFSR/HFSR, no DWT cycle counter, word-only NVIC_IPR) ---- */
#define SYST_CSR     REG(0xE000E010u)
#define SYST_RVR     REG(0xE000E014u)
#define SYST_CVR     REG(0xE000E018u)
#define NVIC_ISER0   REG(0xE000E100u)
#define NVIC_ICER0   REG(0xE000E180u)
#define NVIC_ICPR0   REG(0xE000E280u)
#define NVIC_IPR(n)  REG(0xE000E400u + 4u * ((n) >> 2))
#define SCB_VTOR     REG(0xE000ED08u)
#define SCB_AIRCR    REG(0xE000ED0Cu)

#define UID_BASE     0x1FFF7550u
#define SYSCLK_HZ    48000000u
#define BAUD         115200u

#define AIRCR_SYSRESETREQ 0x05FA0004u

/* ---- hw.c ---- */
void hw_init(void);                 /* clock 48 MHz, GPIO, USART1+USART2, TIM14 time base */
void nvic_set_prio(uint32_t irq, uint32_t prio);   /* prio 0..3 (2 bits on the M0+) */
/* Every output byte goes to both UARTs, in the same order (SPEC-ESP32 1). */
void uart_putc(char c);
void uart_puts(const char *s);
void uart_hex32(uint32_t v);
void uart_u32(uint32_t v);
void uart_i32(int32_t v);
void uart_nl(void);
void uart_line(const char *s);      /* s + \r\n */
void uart_flush(void);              /* wait for TC on both UARTs */
void uart_version(uint32_t v);      /* "x.y.z" from (maj<<16)|(min<<8)|patch */
void uart_reset_cause(uint32_t csr);
/* Line input from both UARTs, one line buffer per UART (SPEC-ESP32 1). Bytes are moved from the
 * receivers into small per-UART rings whenever the firmware polls or waits to transmit, then into
 * the line buffer of that UART. line_poll() returns:
 *   0   nothing complete yet
 *   >0  a complete line (length) on UART *u (0 = USART1, 1 = USART2), NUL-terminated in
 *       line_buf(*u); valid until the next line_poll()
 *   -2  a bad line on UART *u: longer than `max`, a NUL byte, or a lost byte (overrun) */
/* One buffer per UART (USART1, USART2), each max + 1 bytes. */
void line_init(char *buf_u1, char *buf_u2, uint32_t max);
int  line_poll(uint32_t *u);
char *line_buf(uint32_t u);
void uart_rx_service(void);         /* move received bytes into the rings (non-blocking) */

/* 1 MHz time base (TIM14 + overflow count, RAM ISR). Wraps after ~71 minutes. */
uint32_t time_us(void);
static inline uint32_t us_since(uint32_t t0) { return time_us() - t0; }
void delay_ms(uint32_t ms);
void iwdg_start_2s(void);
void iwdg_feed(void);
void system_reset(void) __attribute__((noreturn));
void led_set(int on);
void led_toggle(void);
/* Stops TIM14 and its interrupt (BL, right before the jump; the app restarts it). */
void timebase_stop(void);

/* string helpers (no libc) */
int  str_eq(const char *a, const char *b);
int  starts_with(const char *s, const char *p);
int  parse_i32(const char **s, int32_t *out);
int  parse_u32(const char **s, uint32_t *out);
int  parse_hex32(const char **s, uint32_t *out);
const char *next_token(const char **s, uint32_t *len);
void mem_copy(void *d, const void *s, uint32_t n);
int  mem_eq(const void *a, const void *b, uint32_t n);
static inline uint32_t rd32le(const uint8_t *p) {   /* byte access: no unaligned load */
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* ---- crc32.c / sha256.c / b64.c (identical to firmware/boot) ---- */
uint32_t crc32_update(uint32_t crc, const void *data, uint32_t len);  /* start with 0 */
uint32_t crc32(const void *data, uint32_t len);

typedef struct {
    uint32_t h[8];
    uint8_t  buf[64];
    uint32_t buflen;
    uint64_t total;
} sha256_ctx_t;
void sha256_init(sha256_ctx_t *c);
void sha256_update(sha256_ctx_t *c, const void *data, uint32_t len);
void sha256_final(sha256_ctx_t *c, uint8_t out[32]);

int  b64_decode(const char *in, uint32_t n, uint8_t *out, uint32_t out_max);

/* ---- flash.c ---- */
int  flash_erase_pages(uint32_t first, uint32_t n);                 /* 0 = OK */
int  flash_program(uint32_t addr, const void *data, uint32_t len);  /* addr, len multiples of 8 */
int  flash_is_erased(uint32_t addr, uint32_t len);

/* ---- image.c ---- */
typedef struct {
    uint32_t magic;
    uint16_t header_version;
    uint16_t flags;
    uint32_t board_id;
    uint32_t fw_version;
    uint32_t security_counter;
    uint32_t link_addr;
    uint32_t payload_size;
    uint32_t payload_crc32;
    uint8_t  payload_sha256[32];
} img_hdr_t;
_Static_assert(sizeof(img_hdr_t) == 64, "header is 64 bytes");

enum { IMG_OK = 0, IMG_BAD_HDR, IMG_BOARD, IMG_SIZE, IMG_LINK, IMG_CRC };
/* Header-only check of a 256-byte header for the given slot (0=A, 1=B). Byte access only. */
int  img_check_header(const uint8_t *hdr, uint32_t slot);
/* Full check of what is in flash (header + payload CRC). */
int  img_check_slot(uint32_t slot);
int  slot_is_empty(uint32_t slot);
/* Slot bases are 8 KB aligned, so this cast is aligned. */
static inline const img_hdr_t *slot_hdr(uint32_t slot) { return (const img_hdr_t *)SLOT_BASE(slot); }

/* ---- meta.c (A/B only, also used by the apps) ---- */
enum { META_CONFIRMED = 1, META_TRIAL = 2, META_ATTEMPT = 3, META_CONFIRM = 4,
       META_ROLLBACK = 5, META_REPAIR = 6 };
#define META_NONE 0xFFu
typedef struct {
    uint32_t magic;
    uint32_t seq;
    uint8_t  kind;
    uint8_t  active;
    uint8_t  trial;
    uint8_t  attempts;
    uint32_t min_security_counter;
    uint32_t erase_self;
    uint32_t erase_other;
    uint32_t reserved;
    uint32_t crc32;
} meta_rec_t;
_Static_assert(sizeof(meta_rec_t) == META_REC_SIZE, "meta record is 32 bytes");

typedef struct {
    int      valid;          /* a valid record was found */
    meta_rec_t rec;          /* latest record */
    uint32_t sector_idx;     /* 0 = META0, 1 = META1: where the latest record is */
    uint32_t erases[2];      /* erase counts of META0, META1 */
} meta_state_t;

void meta_load(meta_state_t *st);
int  meta_append(meta_state_t *st, uint8_t kind, uint8_t active, uint8_t trial,
                 uint8_t attempts, uint32_t min_sc);

/* ---- fault.c ---- */
enum fault_phase { PH_ERASE = 0, PH_DATA, PH_HDR, PH_META, PH_ROTATE, PH_BOOT, PH_JUMP,
                   PH_CONFIRM, PH_COUNT };
const char *phase_name(uint32_t p);
uint32_t fault_point(uint32_t phase, int32_t index);   /* returns a time_us() stamp */
void fault_trace(uint32_t phase, int32_t index, uint32_t t0);
int  fault_command(const char *line);
int  fault_timer_pending(void);
void fault_boot_snapshot(void);
void fault_print_report(void);

/* ---- startup ---- */
extern const uint32_t g_vectors[];

#endif
