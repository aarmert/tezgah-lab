/*
 * Flash driver (RM0090 section 3). The busy-wait parts run from RAM (.ramfunc): on the F407 a CPU
 * fetch from flash stalls while the flash is erasing/programming, and the TIM2 fault ISR (also in
 * RAM, via the RAM vector table that VTOR points to from startup on) must be able to fire in the
 * middle of an erase (SPEC section 5).
 *
 * Safety rails: only sectors 1..4 can be erased, only 0x08004000..0x0801FFFF can be programmed.
 * MER (mass erase), option bytes and RDP are never touched.
 */
#include "common.h"

#define CR_PG        (1u << 0)
#define CR_SER       (1u << 1)
#define CR_SNB(n)    ((n) << 3)
#define CR_PSIZE_X32 (2u << 8)
#define CR_STRT      (1u << 16)
#define CR_LOCK      (1u << 31)
#define SR_BSY       (1u << 16)
#define SR_ERRORS    0xF2u          /* OPERR, WRPERR, PGAERR, PGPERR, PGSERR */
#define SR_CLEAR     0xF3u          /* errors + EOP (write 1 to clear) */

#define PROG_MIN     META0_BASE
#define PROG_END     (META1_BASE + META1_SIZE)

/* ---- RAM part: no calls, no flash literals outside this section ---- */
RAMFUNC static uint32_t ram_erase(uint32_t sector) {
    while (FLASH_SR & SR_BSY) { }
    FLASH_SR = SR_CLEAR;
    FLASH_CR = CR_PSIZE_X32 | CR_SER | CR_SNB(sector);
    FLASH_CR = CR_PSIZE_X32 | CR_SER | CR_SNB(sector) | CR_STRT;
    __asm volatile("dsb");
    while (FLASH_SR & SR_BSY) { }
    uint32_t sr = FLASH_SR;
    FLASH_CR = 0;
    return sr & SR_ERRORS;
}

/* Programs words in ascending address order: the last word of a record/header is written last. */
RAMFUNC static uint32_t ram_program(volatile uint32_t *dst, const uint32_t *src, uint32_t words) {
    uint32_t err = 0;
    while (FLASH_SR & SR_BSY) { }
    FLASH_SR = SR_CLEAR;
    FLASH_CR = CR_PSIZE_X32 | CR_PG;
    for (uint32_t i = 0; i < words; i++) {
        dst[i] = src[i];
        __asm volatile("dsb");
        while (FLASH_SR & SR_BSY) { }
        err = FLASH_SR & SR_ERRORS;
        if (err) break;
    }
    FLASH_CR = 0;
    return err;
}

/* ---- Flash part ---- */
static void unlock(void) {
    if (FLASH_CR & CR_LOCK) {
        FLASH_KEYR = 0x45670123u;
        FLASH_KEYR = 0xCDEF89ABu;
    }
}

static void lock(void) { FLASH_CR = CR_LOCK; }

int flash_erase_sector(uint32_t sector) {
    if (sector < SECTOR_MIN_ERASABLE || sector > SECTOR_MAX_ERASABLE) return -1;
    unlock();
    uint32_t err = ram_erase(sector);
    lock();
    return err ? -2 : 0;
}

int flash_program(uint32_t addr, const void *data, uint32_t len) {
    if ((addr & 3u) || (len & 3u) || addr < PROG_MIN || addr + len > PROG_END || addr + len < addr)
        return -1;
    /* Source must be word-aligned for the RAM loop; copy through a small aligned buffer. */
    uint32_t buf[16];
    const uint8_t *s = data;
    unlock();
    uint32_t err = 0;
    while (len && !err) {
        uint32_t n = len > sizeof buf ? sizeof buf : len;
        mem_copy(buf, s, n);
        err = ram_program((volatile uint32_t *)addr, buf, n / 4u);
        addr += n; s += n; len -= n;
    }
    lock();
    return err ? -2 : 0;
}

int flash_is_erased(uint32_t addr, uint32_t len) {
    const volatile uint32_t *p = (const volatile uint32_t *)addr;
    for (uint32_t i = 0; i < len / 4u; i++) if (p[i] != 0xFFFFFFFFu) return 0;
    return 1;
}
