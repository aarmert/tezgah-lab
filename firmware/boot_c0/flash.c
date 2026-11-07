/*
 * Flash driver for the STM32C031 (RM0490, "Embedded flash memory": 2 KB pages, 64-bit
 * double-word programming, no ECC). The busy-wait parts run from RAM (.ramfunc): a CPU fetch from
 * flash stalls while the flash is erasing/programming, and the TIM3 fault ISR (also in RAM, via
 * the RAM vector table VTOR points to from startup on) must be able to fire in the middle of an
 * erase (SPEC 5).
 *
 * Sequence (both operations): wait until BSY1 and CFGBSY are clear, clear every error flag (and
 * EOP), set PG or PER+PNB (+STRT), wait for BSY1 and CFGBSY again, read the error flags, clear
 * PG/PER. EOP is only set when EOPIE is enabled, so success is "no error flag" plus the caller's
 * read-back. A double-word is programmed by writing word 0 and then word 1 (the second write starts
 * the operation); the pair is written with interrupts masked, so no handler runs in between. A
 * double-word that is not erased is never programmed (that would be PROGERR). After every
 * operation the instruction cache is reset (ICEN off, ICRST pulse, ICEN back on).
 *
 * Safety rails: only pages 6..15 can be erased, only 0x08003000..0x08007FFF can be programmed.
 * There is no mass erase and no option byte access here at all.
 */
#include "common.h"

#define CR_PG        (1u << 0)
#define CR_PER       (1u << 1)
#define CR_PNB(n)    ((n) << 3)
#define CR_STRT      (1u << 16)
#define CR_LOCK      (1u << 31)
#define SR_EOP       (1u << 0)
#define SR_OPERR     (1u << 1)
#define SR_PROGERR   (1u << 3)
#define SR_WRPERR    (1u << 4)
#define SR_PGAERR    (1u << 5)
#define SR_SIZERR    (1u << 6)
#define SR_PGSERR    (1u << 7)
#define SR_MISSERR   (1u << 8)
#define SR_FASTERR   (1u << 9)
#define SR_RDERR     (1u << 14)
#define SR_BSY1      (1u << 16)
#define SR_CFGBSY    (1u << 18)
#define SR_ERRORS    (SR_OPERR | SR_PROGERR | SR_WRPERR | SR_PGAERR | SR_SIZERR | SR_PGSERR | \
                      SR_MISSERR | SR_FASTERR | SR_RDERR)
#define SR_CLEAR     (SR_ERRORS | SR_EOP)
#define SR_BUSY      (SR_BSY1 | SR_CFGBSY)

#define PROG_MIN     (FLASH_BASE + PAGE_MIN_ERASABLE * PAGE_SIZE)          /* META0 */
#define PROG_END     (FLASH_BASE + (PAGE_MAX_ERASABLE + 1u) * PAGE_SIZE)   /* end of slot B */

/* ---- RAM part: no calls, no flash literals outside this section ---- */
RAMFUNC static void ram_icache_reset(void) {
    uint32_t acr = FLASH_ACR;
    FLASH_ACR = acr & ~ACR_ICEN;
    FLASH_ACR = (acr & ~ACR_ICEN) | ACR_ICRST;
    FLASH_ACR = acr & ~(ACR_ICEN | ACR_ICRST);
    FLASH_ACR = acr & ~ACR_ICRST;           /* ICEN as it was */
}

RAMFUNC static uint32_t ram_erase(uint32_t first, uint32_t n) {
    uint32_t err = 0;
    for (uint32_t p = first; p < first + n && !err; p++) {
        while (FLASH_SR & SR_BUSY) { }
        FLASH_SR = SR_CLEAR;
        FLASH_CR = CR_PER | CR_PNB(p);
        FLASH_CR = CR_PER | CR_PNB(p) | CR_STRT;
        __asm volatile("dsb");
        while (FLASH_SR & SR_BUSY) { }
        err = FLASH_SR & SR_ERRORS;
        FLASH_CR = 0;
    }
    ram_icache_reset();
    return err;
}

/* Programs double-words in ascending address order: the last double-word of a record/header (the
 * one with its CRC) is programmed last. */
RAMFUNC static uint32_t ram_program(volatile uint32_t *dst, const uint32_t *src, uint32_t dwords) {
    uint32_t err = 0;
    while (FLASH_SR & SR_BUSY) { }
    FLASH_SR = SR_CLEAR;
    FLASH_CR = CR_PG;
    for (uint32_t i = 0; i < dwords; i++) {
        uint32_t pm;
        __asm volatile("mrs %0, primask" : "=r"(pm));
        __asm volatile("cpsid i" ::: "memory");
        dst[2u * i] = src[2u * i];
        dst[2u * i + 1u] = src[2u * i + 1u];          /* starts the double-word program */
        __asm volatile("msr primask, %0" : : "r"(pm) : "memory");
        __asm volatile("dsb");
        while (FLASH_SR & SR_BUSY) { }
        err = FLASH_SR & SR_ERRORS;
        if (err) break;
    }
    FLASH_CR = 0;
    ram_icache_reset();
    return err;
}

/* ---- Flash part ---- */
static void unlock(void) {
    if (FLASH_CR & CR_LOCK) {
        FLASH_KEYR = 0x45670123u;
        FLASH_KEYR = 0xCDEF89ABu;
    }
}

static void lock(void) { FLASH_CR |= CR_LOCK; }

int flash_erase_pages(uint32_t first, uint32_t n) {
    if (n == 0 || first < PAGE_MIN_ERASABLE || first > PAGE_MAX_ERASABLE ||
        n > PAGE_MAX_ERASABLE + 1u - first)
        return -1;
    unlock();
    uint32_t err = ram_erase(first, n);
    lock();
    return err ? -2 : 0;
}

int flash_program(uint32_t addr, const void *data, uint32_t len) {
    if ((addr & (PROG_UNIT - 1u)) || (len & (PROG_UNIT - 1u)) || addr < PROG_MIN ||
        addr + len > PROG_END || addr + len < addr)
        return -1;
    /* Never program a double-word that is not erased (the C0 answers PROGERR): refuse it up
     * front, so a logic error shows as ERR FLASH and never as a half-changed double-word. */
    if (!flash_is_erased(addr, len)) return -3;
    /* The RAM loop needs a word-aligned source; copy through a small aligned buffer. */
    uint32_t buf[16];
    const uint8_t *s = data;
    unlock();
    uint32_t err = 0;
    while (len && !err) {
        uint32_t n = len > sizeof buf ? sizeof buf : len;
        mem_copy(buf, s, n);
        err = ram_program((volatile uint32_t *)addr, buf, n / PROG_UNIT);
        addr += n; s += n; len -= n;
    }
    lock();
    return err ? -2 : 0;
}

int flash_is_erased(uint32_t addr, uint32_t len) {
    const volatile uint32_t *p = (const volatile uint32_t *)addr;   /* addr is word aligned */
    for (uint32_t i = 0; i < len / 4u; i++) if (p[i] != 0xFFFFFFFFu) return 0;
    return 1;
}
