/* Image header and slot validation (SPEC 2, SPEC-C031C6 2: 256 B header, CRC at 252, payload size
 * a multiple of 8). Byte access only (ARMv6-M has no unaligned loads). */
#include "common.h"

int img_check_header(const uint8_t *hdr, uint32_t slot) {
    img_hdr_t h;
    mem_copy(&h, hdr, sizeof h);
    if (h.magic != IMG_MAGIC || h.header_version != IMG_HDR_VERSION ||
        crc32(hdr, HDR_CRC_OFF) != rd32le(hdr + HDR_CRC_OFF))
        return IMG_BAD_HDR;
    if (h.board_id != BOARD_ID) return IMG_BOARD;
    if (h.payload_size == 0 || h.payload_size > PAYLOAD_MAX || (h.payload_size & (PROG_UNIT - 1u)))
        return IMG_SIZE;
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
