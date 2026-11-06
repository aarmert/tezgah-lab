/* CRC-32 (IEEE 802.3, reflected, poly 0xEDB88320), identical to zlib.crc32.
 * The 1 KB table is built in RAM on first use (no flash cost, ~8 cycles/byte). */
#include "common.h"

static uint32_t s_table[256];
static int s_ready;

static void crc32_init(void) {
    for (uint32_t i = 0; i < 256u; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        s_table[i] = c;
    }
    s_ready = 1;
}

uint32_t crc32_update(uint32_t crc, const void *data, uint32_t len) {
    if (!s_ready) crc32_init();
    const uint8_t *p = data;
    crc = ~crc;
    while (len--) crc = s_table[(crc ^ *p++) & 0xFFu] ^ (crc >> 8);
    return ~crc;
}

uint32_t crc32(const void *data, uint32_t len) { return crc32_update(0, data, len); }
