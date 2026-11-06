/* Standard base64 decoder (RFC 4648 alphabet, '=' padding required for partial groups). */
#include "common.h"

static int val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

int b64_decode(const char *in, uint32_t n, uint8_t *out, uint32_t out_max) {
    if (n % 4u) return -1;
    uint32_t o = 0;
    for (uint32_t i = 0; i < n; i += 4) {
        int v[4], pad = 0;
        for (int k = 0; k < 4; k++) {
            char c = in[i + (uint32_t)k];
            if (c == '=') {
                /* '=' only in the last group, only in the last two positions */
                if (i + 4u != n || k < 2) return -1;
                v[k] = 0;
                pad++;
            } else {
                if (pad) return -1;
                v[k] = val(c);
                if (v[k] < 0) return -1;
            }
        }
        uint32_t w = ((uint32_t)v[0] << 18) | ((uint32_t)v[1] << 12) | ((uint32_t)v[2] << 6) | (uint32_t)v[3];
        uint32_t cnt = 3u - (uint32_t)pad;
        if (o + cnt > out_max) return -1;
        out[o++] = (uint8_t)(w >> 16);
        if (cnt > 1) out[o++] = (uint8_t)(w >> 8);
        if (cnt > 2) out[o++] = (uint8_t)w;
    }
    return (int)o;
}
