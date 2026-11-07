/*
 * Meta log (SPEC 4.1, SPEC-C031C6 4.1): append-only 32-byte records in META0 (page 6, 2 KB) and
 * META1 (page 7, 2 KB), 64 records each. The valid record with the highest seq (searched in both
 * pages) holds the state. "Sector" below means one of the two meta pages.
 *
 * Append: the record goes right after the last non-erased record slot of the sector holding the
 * latest record (a half-written record is skipped, never overwritten or re-programmed). A record is
 * 4 double-words programmed in ascending order, so the double-word holding the CRC is programmed
 * last.
 *
 * Rotation: when that sector is full, erase the other sector, then write the new record (a full
 * state snapshot) at its start with that sector's erase_count + 1. The old sector stays intact
 * until the next rotation, so a reset during rotation keeps the old state.
 */
#include "common.h"

static const uint32_t SEC_BASE[2] = {META0_BASE, META1_BASE};
static const uint32_t SEC_SIZE[2] = {META0_SIZE, META1_SIZE};
static const uint32_t SEC_NUM[2]  = {META0_PAGE, META1_PAGE};

static int rec_valid(const meta_rec_t *r) {
    return r->magic == META_MAGIC && crc32(r, 28) == r->crc32;
}

static int rec_erased(const meta_rec_t *r) {
    return flash_is_erased((uint32_t)(uintptr_t)r, META_REC_SIZE);
}

/* Index of the first record slot after the last non-erased one (0 = sector empty). */
static uint32_t next_free(uint32_t s) {
    const meta_rec_t *recs = (const meta_rec_t *)SEC_BASE[s];
    uint32_t n = SEC_SIZE[s] / META_REC_SIZE;
    while (n && rec_erased(&recs[n - 1])) n--;
    return n;
}

/* Latest (= last-written) valid record of a sector, or 0. Seqs grow in append order. */
static const meta_rec_t *latest_in(uint32_t s) {
    const meta_rec_t *recs = (const meta_rec_t *)SEC_BASE[s];
    for (uint32_t i = next_free(s); i > 0; i--)
        if (rec_valid(&recs[i - 1])) return &recs[i - 1];
    return 0;
}

void meta_load(meta_state_t *st) {
    const meta_rec_t *r0 = latest_in(0), *r1 = latest_in(1);
    const meta_rec_t *best = r0;
    uint32_t s = 0;
    if (r1 && (!r0 || r1->seq > r0->seq)) { best = r1; s = 1; }
    if (!best) {
        st->valid = 0;
        st->sector_idx = 0;
        st->erases[0] = st->erases[1] = 0;
        st->rec.magic = 0;
        st->rec.seq = 0;
        st->rec.kind = 0;
        st->rec.active = META_NONE;
        st->rec.trial = META_NONE;
        st->rec.attempts = 0;
        st->rec.min_security_counter = 0;
        return;
    }
    st->valid = 1;
    mem_copy(&st->rec, best, sizeof st->rec);
    st->sector_idx = s;
    st->erases[s] = best->erase_self;
    st->erases[1u - s] = best->erase_other;
}

int meta_append(meta_state_t *st, uint8_t kind, uint8_t active, uint8_t trial,
                uint8_t attempts, uint32_t min_sc) {
    uint32_t cur = st->valid ? st->sector_idx : 0u;
    uint32_t tgt = cur;
    uint32_t idx = next_free(cur);
    uint32_t erases[2] = {st->erases[0], st->erases[1]};

    if (idx >= SEC_SIZE[cur] / META_REC_SIZE) {
        tgt = 1u - cur;
        int32_t sec = (int32_t)SEC_NUM[tgt];
        uint32_t t0 = fault_point(PH_ROTATE, sec);
        int e = flash_erase_pages(SEC_NUM[tgt], 1u);
        fault_trace(PH_ROTATE, sec, t0);
        if (e) return -1;
        erases[tgt]++;
        idx = 0;
    }

    meta_rec_t r;
    r.magic = META_MAGIC;
    r.seq = st->valid ? st->rec.seq + 1u : 1u;
    r.kind = kind;
    r.active = active;
    r.trial = trial;
    r.attempts = attempts;
    r.min_security_counter = min_sc;
    r.erase_self = erases[tgt];
    r.erase_other = erases[1u - tgt];
    r.reserved = 0xFFFFFFFFu;
    r.crc32 = crc32(&r, 28);

    uint32_t addr = SEC_BASE[tgt] + idx * META_REC_SIZE;
    uint32_t t0 = fault_point(PH_META, kind);
    int e = flash_program(addr, &r, sizeof r);   /* crc32 is in the last double-word -> last */
    fault_trace(PH_META, kind, t0);
    meta_load(st);
    if (e || !mem_eq((const void *)addr, &r, sizeof r)) return -1;
    return 0;
}
