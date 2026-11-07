/*
 * Shared declarations for the bootloaders and the apps (register-level, no HAL).
 * Register style follows firmware/main.c.
 */
#ifndef COMMON_H
#define COMMON_H
#include <stdint.h>
#include <stddef.h>
#include "layout.h"

#define REG(addr) (*(volatile uint32_t *)(addr))

/* Code that must run while the flash is busy (SPEC section 5). long_call: it lives in RAM,
 * far from the flash callers. */
#define RAMFUNC __attribute__((section(".ramfunc"), noinline, long_call))
/* Interrupt handlers in RAM (referenced by address from the vector table, never called) */
#define RAMISR  __attribute__((section(".ramfunc"), noinline, used))

/* ---- RCC ---- */
#define RCC_BASE     0x40023800u
#define RCC_AHB1ENR  REG(RCC_BASE + 0x30)
#define RCC_APB1ENR  REG(RCC_BASE + 0x40)
#define RCC_CSR      REG(RCC_BASE + 0x74)

/* ---- GPIO ---- */
#define GPIOA_BASE   0x40020000u
#define GPIOD_BASE   0x40020C00u
#define GPIO_MODER(b)   REG((b) + 0x00)
#define GPIO_PUPDR(b)   REG((b) + 0x0C)
#define GPIO_ODR(b)     REG((b) + 0x14)
#define GPIO_BSRR(b)    REG((b) + 0x18)
#define GPIO_AFRL(b)    REG((b) + 0x20)

/* ---- USART2 ---- */
#define USART2_BASE  0x40004400u
#define USART_SR     REG(USART2_BASE + 0x00)
#define USART_DR     REG(USART2_BASE + 0x04)
#define USART_BRR    REG(USART2_BASE + 0x08)
#define USART_CR1    REG(USART2_BASE + 0x0C)
#define SR_ORE       (1u << 3)
#define SR_RXNE      (1u << 5)
#define SR_TC        (1u << 6)
#define SR_TXE       (1u << 7)

/* ---- Flash interface ---- */
#define FLASH_IF     0x40023C00u
#define FLASH_ACR    REG(FLASH_IF + 0x00)
#define FLASH_KEYR   REG(FLASH_IF + 0x04)
#define FLASH_SR     REG(FLASH_IF + 0x0C)
#define FLASH_CR     REG(FLASH_IF + 0x10)

/* ---- TIM2 (32 bit, APB1 = 16 MHz) ---- */
#define TIM2_BASE    0x40000000u
#define TIM2_CR1     REG(TIM2_BASE + 0x00)
#define TIM2_DIER    REG(TIM2_BASE + 0x0C)
#define TIM2_SR      REG(TIM2_BASE + 0x10)
#define TIM2_EGR     REG(TIM2_BASE + 0x14)
#define TIM2_CNT     REG(TIM2_BASE + 0x24)
#define TIM2_PSC     REG(TIM2_BASE + 0x28)
#define TIM2_ARR     REG(TIM2_BASE + 0x2C)
#define TIM2_IRQN    28u

/* ---- IWDG ---- */
#define IWDG_BASE    0x40003000u
#define IWDG_KR      REG(IWDG_BASE + 0x00)
#define IWDG_PR      REG(IWDG_BASE + 0x04)
#define IWDG_RLR     REG(IWDG_BASE + 0x08)
#define IWDG_SR      REG(IWDG_BASE + 0x0C)

/* ---- Core ---- */
#define SYST_CSR     REG(0xE000E010u)
#define SYST_RVR     REG(0xE000E014u)
#define SYST_CVR     REG(0xE000E018u)
#define NVIC_ISER0   REG(0xE000E100u)
#define NVIC_ICER0   REG(0xE000E180u)
#define NVIC_ICPR0   REG(0xE000E280u)
#define NVIC_IPR_B(n) (*(volatile uint8_t *)(0xE000E400u + (n)))
#define SCB_VTOR     REG(0xE000ED08u)
#define SCB_AIRCR    REG(0xE000ED0Cu)
#define SCB_CFSR     REG(0xE000ED28u)
#define SCB_HFSR     REG(0xE000ED2Cu)
#define DEMCR        REG(0xE000EDFCu)
#define DWT_CTRL     REG(0xE0001000u)
#define DWT_CYCCNT   REG(0xE0001004u)

#define UID_BASE     0x1FFF7A10u
#define HSI_HZ       16000000u
#define CYC_PER_US   (HSI_HZ / 1000000u)
#define BAUD         115200u

#define AIRCR_SYSRESETREQ 0x05FA0004u

/* ---- hw.c ---- */
void hw_init(void);                 /* clocks, GPIO, USART2, DWT */
void uart_putc(char c);
void uart_puts(const char *s);
void uart_hex32(uint32_t v);
void uart_hex8(uint8_t v);
void uart_u32(uint32_t v);
void uart_i32(int32_t v);
void uart_nl(void);
void uart_line(const char *s);      /* s + \r\n */
void uart_flush(void);              /* wait for TC */
void uart_version(uint32_t v);      /* "x.y.z" from (maj<<16)|(min<<8)|patch */
void uart_reset_cause(uint32_t csr);
/* Reads one line (without \r/\n) into buf. Returns its length, -1 on timeout (no complete line
 * within timeout_ms of inactivity), -2 if the line was longer than max (rest discarded). */
int  uart_getline(char *buf, uint32_t max, uint32_t timeout_ms);
int  uart_poll_char(char *c);       /* non-blocking */
uint32_t dwt_now(void);             /* raw cycle counter */
uint32_t dwt_us_since(uint32_t t0);
void delay_ms(uint32_t ms);
void iwdg_start_2s(void);
void iwdg_feed(void);
void system_reset(void) __attribute__((noreturn));
void led_set(uint32_t idx, int on);

/* string helpers (no libc) */
int  str_eq(const char *a, const char *b);
int  starts_with(const char *s, const char *p);
/* Parses a decimal (optionally negative) number terminated by ' ' or '\0'; advances *s past it
 * and past one following space. Returns 1 on success. */
int  parse_i32(const char **s, int32_t *out);
int  parse_u32(const char **s, uint32_t *out);
int  parse_hex32(const char **s, uint32_t *out);
const char *next_token(const char **s, uint32_t *len);
void mem_copy(void *d, const void *s, uint32_t n);
int  mem_eq(const void *a, const void *b, uint32_t n);

/* ---- crc32.c / sha256.c / b64.c ---- */
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

/* Decodes standard base64 (with '=' padding) of length n. Returns the byte count, or -1. */
int  b64_decode(const char *in, uint32_t n, uint8_t *out, uint32_t out_max);

/* ---- flash.c ---- */
int  flash_erase_sector(uint32_t sector);                        /* 0 = OK */
int  flash_program(uint32_t addr, const void *data, uint32_t len); /* len multiple of 4; 0 = OK */
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
#define HDR_CRC_OFF 508u

enum { IMG_OK = 0, IMG_BAD_HDR, IMG_BOARD, IMG_SIZE, IMG_LINK, IMG_CRC };
/* Header-only check of a 512-byte header for the given slot (0=A, 1=B). */
int  img_check_header(const uint8_t *hdr512, uint32_t slot);
/* Full check of what is in flash (header + payload CRC). */
int  img_check_slot(uint32_t slot);
int  slot_is_empty(uint32_t slot);
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
/* Appends a record of `kind` with the given state (seq, erase counts and CRC are filled in),
 * rotating sectors when needed, and reloads *st. Returns 0 on success. */
int  meta_append(meta_state_t *st, uint8_t kind, uint8_t active, uint8_t trial,
                 uint8_t attempts, uint32_t min_sc);

/* ---- fault.c ---- */
enum fault_phase { PH_ERASE = 0, PH_DATA, PH_HDR, PH_META, PH_ROTATE, PH_BOOT, PH_JUMP,
                   PH_CONFIRM, PH_COUNT };
const char *phase_name(uint32_t p);
/* Start of an instrumented operation. Fires an armed fault if one matches. Returns a DWT stamp. */
uint32_t fault_point(uint32_t phase, int32_t index);
/* End of the operation: prints "T <PHASE> <idx> <us>" when tracing is on. */
void fault_trace(uint32_t phase, int32_t index, uint32_t t0);
/* Handles a "FAULT ..." line. Returns 1 if the line was a FAULT command (handled, reply printed),
 * 0 otherwise. Without FAULT_HOOKS it always returns 0. */
int  fault_command(const char *line);
int  fault_timer_pending(void);
/* BL entry, before the BOOT fault point: decide whether the last started fault timer expired
 * (fired) or not (cancelled) and take a snapshot of both counts. Nothing is cleared here, so a BOOT
 * fault that resets on the spot loses nothing. */
void fault_boot_snapshot(void);
/* After the status line: "BL fault_fired n PHASE idx delay" and "BL fault_cancelled n PHASE idx
 * delay" (n since the last report, details of the last one), then clears what was reported. */
void fault_print_report(void);

/* ---- startup ---- */
extern const uint32_t g_vectors[];
void vectors_to_ram(void);

#endif
