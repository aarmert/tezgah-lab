/*
 * Update protocol in the BL (SPEC section 4, SPEC-C031C6 4). Stop-and-wait, one line in, one
 * reply out. Lines are accepted on both UARTs (separate line buffers, SPEC-ESP32 1); every reply
 * goes to both. The UARTs are not read while the flash is busy; the host never pipelines.
 *
 * C0 differences: one 400-char limit for every line; DATA `off` and the decoded length must be
 * multiples of 8 (else ERR SEQ <expected>); the 3 s inactivity timer restarts on every line that
 * is not answered with ERR LINE (a stream of garbage does not keep the BL in update mode).
 *
 * A/B commit order: payload -> END reads the flash back and checks CRC32 + SHA-256 -> header
 * written last (its CRC word last) -> TRIAL meta record (its CRC word last) -> OK COMMIT -> reset.
 *
 * naive (-DDESIGN_NAIVE): BEGIN erases slot A in place, END writes the header with no check.
 */
#include "bl.h"
#include "noinit.h"

#define CHUNK_MAX      192u
#define UPD_TIMEOUT_MS 3000u

static struct {
    int      active;          /* a BEGIN succeeded and no END/ABORT yet */
    uint32_t slot;
    uint32_t size;
    uint32_t written;         /* contiguous bytes programmed from the payload start */
    uint32_t chunks;          /* DATA chunks programmed (fault point index) */
    uint32_t hdr[HDR_SIZE / 4u];
} x;

static uint32_t s_chunk32[CHUNK_MAX / 4u];        /* aligned */
#define s_chunk ((uint8_t *)s_chunk32)
static int s_err_line;                            /* this line was answered with ERR LINE */

static void err_line(void) {
    s_err_line = 1;
    uart_line("ERR LINE");
}

static void set_in_progress(uint32_t v) {
#ifdef DESIGN_NAIVE
    (void)v;            /* naive keeps no resume state (SPEC 4) */
    return;
#endif
    if (g_noinit.update_in_progress != v) {
        g_noinit.update_in_progress = v;
        noinit_commit();
    }
}

static void reply_err_off(const char *code, uint32_t n) {
    uart_puts("ERR "); uart_puts(code); uart_putc(' '); uart_u32(n); uart_nl();
}

static void xfer_fail(const char *line) {
    x.active = 0;
    set_in_progress(0);
    uart_line(line);
}

#ifndef DESIGN_NAIVE
static uint32_t target_slot(void) {
    /* the inactive slot; with no meta record (nofw), slot A */
    return (g_meta.valid && g_meta.rec.active == 0u) ? 1u : 0u;
}
static uint32_t min_sc(void) { return g_meta.valid ? g_meta.rec.min_security_counter : 0u; }
#endif

static void cmd_begin(const char *b64) {
    uint32_t len = 0;
    while (b64[len] && b64[len] != ' ') len++;
    x.active = 0;
    uint8_t *hdr = (uint8_t *)x.hdr;
    if (b64[len] != '\0' || b64_decode(b64, len, hdr, HDR_SIZE) != (int)HDR_SIZE) {
        uart_line("ERR BAD_HDR");
        return;
    }
    const img_hdr_t *h = (const img_hdr_t *)hdr;
#ifndef DESIGN_NAIVE
    uint32_t slot = target_slot();
    switch (img_check_header(hdr, slot)) {
    case IMG_OK: break;
    case IMG_BOARD: uart_line("ERR BOARD"); return;
    case IMG_SIZE:  uart_line("ERR SIZE"); return;
    case IMG_LINK:  uart_line("ERR LINK"); return;
    default:        uart_line("ERR BAD_HDR"); return;
    }
    if (h->security_counter < min_sc()) { uart_line("ERR VERSION"); return; }
#else
    /* naive: only what is needed to stay inside slot A (no board/link/version check) */
    uint32_t slot = 0;
    if (h->magic != IMG_MAGIC || crc32(hdr, HDR_CRC_OFF) != rd32le(hdr + HDR_CRC_OFF)) {
        uart_line("ERR BAD_HDR");
        return;
    }
    if (h->payload_size == 0 || h->payload_size > PAYLOAD_MAX || (h->payload_size & (PROG_UNIT - 1u))) {
        uart_line("ERR SIZE");
        return;
    }
#endif
    set_in_progress(1);
    /* One fault point for the whole slot erase (4 pages); index = the slot's first page. */
    int32_t pg = (int32_t)SLOT_PAGE(slot);
    uint32_t t0 = fault_point(PH_ERASE, pg);
    int e = flash_erase_pages(SLOT_PAGE(slot), SLOT_PAGES);
    uint32_t us = us_since(t0);
    fault_trace(PH_ERASE, pg, t0);
    if (e) { xfer_fail("ERR FLASH"); return; }
    x.active = 1;
    x.slot = slot;
    x.size = h->payload_size;
    x.written = 0;
    x.chunks = 0;
    uart_puts("OK ERASED "); uart_u32(us); uart_nl();
}

/* DATA check order (SPEC 4 with the SPEC-C031C6 4 override, the simulator mirrors it):
 * malformed -> ERR LINE, no session -> ERR STATE, bad base64/CRC -> ERR CRC <off>, off or length
 * not a multiple of 8, or off beyond the next expected offset -> ERR SEQ <written>, past the image
 * size -> ERR SIZE. */
static void cmd_data(const char *p) {
    uint32_t off, crc, blen;
    if (!parse_u32(&p, &off)) { err_line(); return; }
    const char *b64 = next_token(&p, &blen);
    if (!blen || !parse_hex32(&p, &crc) || *p) { err_line(); return; }
    if (!x.active) { uart_line("ERR STATE"); return; }
    int n = b64_decode(b64, blen, s_chunk, CHUNK_MAX);
    if (n <= 0 || crc32(s_chunk, (uint32_t)n) != crc) { reply_err_off("CRC", off); return; }
    uint32_t len = (uint32_t)n;
    if ((off & (PROG_UNIT - 1u)) || (len & (PROG_UNIT - 1u)) || off > x.written) {
        reply_err_off("SEQ", x.written);
        return;
    }
    if (off + len > x.size) { uart_line("ERR SIZE"); return; }

    uint32_t addr = PAYLOAD_ADDR(x.slot) + off;
    if (off < x.written) {
        /* Retry after a lost reply: OK again if it is already there, byte for byte */
        if (off + len <= x.written && mem_eq((const void *)addr, s_chunk, len)) {
            uart_puts("OK "); uart_u32(off); uart_nl();
        } else {
            reply_err_off("SEQ", x.written);
        }
        return;
    }
    uint32_t t0 = fault_point(PH_DATA, (int32_t)x.chunks);
    int e = flash_program(addr, s_chunk, len);
    fault_trace(PH_DATA, (int32_t)x.chunks, t0);
    if (e || !mem_eq((const void *)addr, s_chunk, len)) { xfer_fail("ERR FLASH"); return; }
    x.written += len;
    x.chunks++;
    uart_puts("OK "); uart_u32(off); uart_nl();
}

static void cmd_end(void) {
    if (!x.active) { uart_line("ERR STATE"); return; }
    uint32_t base = SLOT_BASE(x.slot);
#ifndef DESIGN_NAIVE
    const img_hdr_t *h = (const img_hdr_t *)x.hdr;
    if (x.written < x.size) { xfer_fail("ERR SHORT"); return; }
    const uint8_t *payload = (const uint8_t *)(base + HDR_SIZE);
    if (crc32(payload, x.size) != h->payload_crc32) { xfer_fail("ERR IMG_CRC"); return; }
    sha256_ctx_t sc;
    uint8_t dig[32];
    sha256_init(&sc);
    sha256_update(&sc, payload, x.size);
    sha256_final(&sc, dig);
    if (!mem_eq(dig, h->payload_sha256, 32)) { xfer_fail("ERR IMG_SHA"); return; }
#endif
    /* Header last: its CRC word (offset 252) is in the last double-word programmed. */
    uint32_t t0 = fault_point(PH_HDR, 0);
    int e = flash_program(base, x.hdr, HDR_SIZE);
    fault_trace(PH_HDR, 0, t0);
    if (e || !mem_eq((const void *)base, x.hdr, HDR_SIZE)) { xfer_fail("ERR FLASH"); return; }
#ifndef DESIGN_NAIVE
    /* Then the TRIAL record (CRC word last). Until it is complete the old record rules. */
    if (meta_append(&g_meta, META_TRIAL, g_meta.valid ? g_meta.rec.active : META_NONE,
                    (uint8_t)x.slot, 0, min_sc())) {
        xfer_fail("ERR FLASH");
        return;
    }
#endif
    x.active = 0;
    set_in_progress(0);
    uart_line("OK COMMIT");
    system_reset();
}

void bl_update_mode(int mode) {
    x.active = 0;
    uint32_t t_last = time_us();
    for (;;) {
        uint32_t u;
        int n = line_poll(&u);
        if (n == 0) {
            if (mode != UPD_NOFW && us_since(t_last) >= UPD_TIMEOUT_MS * 1000u) {
                /* 3 s without a command: give up this update and boot normally */
                x.active = 0;
                set_in_progress(0);
                uart_line("BL timeout");
                return;
            }
            continue;
        }
        s_err_line = 0;
        if (n < 0) {                           /* longer than 400 chars, NUL byte, lost byte */
            err_line();
            continue;
        }

        const char *l = line_buf(u);
        if (starts_with(l, "UPD DATA ")) {
            cmd_data(l + 9);
        } else if (starts_with(l, "UPD BEGIN ")) {
            cmd_begin(l + 10);
        } else if (str_eq(l, "UPD END")) {
            cmd_end();
        } else if (str_eq(l, "UPD ABORT")) {
            x.active = 0;
            set_in_progress(0);
            uart_line("OK ABORT");
            return;                            /* no reset: the boot decision continues */
        } else if (str_eq(l, "PING")) {
            uart_line("PONG");
        } else if (fault_command(l)) {
            /* handled */
        } else {
            err_line();                        /* SPEC 4: any unrecognised line in update mode */
        }
        if (!s_err_line) t_last = time_us();
    }
}
