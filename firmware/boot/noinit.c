#include "common.h"
#include "noinit.h"

__attribute__((section(".noinit"))) noinit_t g_noinit;

static uint32_t noinit_crc(void) {
    return crc32(&g_noinit, (uint32_t)offsetof(noinit_t, crc32));
}

int noinit_check(void) {
    if (g_noinit.magic == NOINIT_MAGIC && g_noinit.crc32 == noinit_crc()) return 1;
    uint8_t *p = (uint8_t *)&g_noinit;
    for (uint32_t i = 0; i < sizeof g_noinit; i++) p[i] = 0;
    g_noinit.magic = NOINIT_MAGIC;
    noinit_commit();
    return 0;
}

void noinit_commit(void) { g_noinit.crc32 = noinit_crc(); }
