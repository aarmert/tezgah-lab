#include "common.h"
#include "noinit.h"

__attribute__((section(".noinit"))) noinit_t g_noinit;

/* PWRRSTF (POR/PDR/BOR) and OBLRSTF: the RAM content is not to be trusted. */
#define CSR_POR_BOR (CSR2_PWRRSTF | CSR2_OBLRSTF)

static uint32_t noinit_crc(void) {
    return crc32(&g_noinit, (uint32_t)offsetof(noinit_t, crc32));
}

int noinit_check(void) {
    /* Power-on: RAM content is garbage (or stale); the expiry record is not trusted. */
    if (RCC_CSR2 & CSR_POR_BOR) {
        uint8_t *x = (uint8_t *)&g_noinit.expiry;
        for (uint32_t i = 0; i < sizeof g_noinit.expiry; i++) x[i] = 0;
    }
    if (g_noinit.magic == NOINIT_MAGIC && g_noinit.crc32 == noinit_crc()) return 1;
    uint8_t *p = (uint8_t *)&g_noinit;
    for (uint32_t i = 0; i < (uint32_t)offsetof(noinit_t, expiry); i++) p[i] = 0;
    g_noinit.magic = NOINIT_MAGIC;
    noinit_commit();
    return 0;
}

void noinit_commit(void) { g_noinit.crc32 = noinit_crc(); }
