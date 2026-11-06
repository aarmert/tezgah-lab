/* Image header and slot validation (SPEC section 2). */
#include "common.h"

int img_check_header(const uint8_t *hdr512, uint32_t slot) {
    img_hdr_t h;
    mem_copy(&h, hdr512, sizeof h);
    uint32_t stored_crc;
    mem_copy(&stored_crc, hdr512 + HDR_CRC_OFF, 4);
    if (h.magic != IMG_MAGIC || h.header_version != IMG_HDR_VERSION ||
        crc32(hdr512, HDR_CRC_OFF) != stored_crc)
        return IMG_BAD_HDR;
    if (h.board_id != BOARD_ID) return IMG_BOARD;
    if (h.payload_size == 0 || h.payload_size > PAYLOAD_MAX || (h.payload_size & 3u)) return IMG_SIZE;
    if (h.link_addr != PAYLOAD_ADDR(slot)) return IMG_LINK;
    return IMG_OK;
}

int img_check_slot(uint32_t slot) {
    const uint8_t *base = (const uint8_t *)SLOT_BASE(slot);
    int r = img_check_header(base, slot);
    if (r != IMG_OK) return r;
    const img_hdr_t *h = slot_hdr(slot);
    if (crc32(base + HDR_SIZE, h->payload_size) != h->payload_crc32) return IMG_CRC;
    return IMG_OK;
}

int slot_is_empty(uint32_t slot) { return flash_is_erased(SLOT_BASE(slot), SLOT_SIZE); }
