/*
 * Shared .noinit RAM area at 0x20000000 (256 B), SPEC section 6.3.
 * The BL and every app reserve it in their link scripts and never zero it.
 * Valid when magic == NOINIT_MAGIC and crc32 matches the bytes before it.
 * The host never reads this struct; it is documented here only.
 */
#ifndef NOINIT_H
#define NOINIT_H
#include <stdint.h>
#include "layout.h"

#define NOINIT_MAGIC 0x4E4F494Eu   /* "NOIN" */

typedef struct {
    uint32_t armed;       /* 1 = armed */
    uint32_t phase;       /* enum fault_phase */
    int32_t  index;       /* -1 = any */
    uint32_t delay_us;
} noinit_fault_t;

typedef struct {
    uint32_t magic;
    uint32_t enter_update;        /* app UPD command -> BL enters update mode */
    uint32_t update_in_progress;  /* set at BEGIN, cleared at COMMIT/ABORT/timeout/END error */
    noinit_fault_t fault[2];
    uint32_t fired_count;
    struct {
        uint32_t phase;
        int32_t  index;
        uint32_t delay_us;
    } last_fired;
    uint32_t trace;               /* 1 = print "T <PHASE> <idx> <us>" lines */
    uint32_t reset_csr;           /* RCC_CSR seen by the BL (it clears the flags; the app prints this) */
    uint32_t crc32;               /* crc32 over all bytes before this field */
} noinit_t;

_Static_assert(sizeof(noinit_t) <= NOINIT_SIZE, "noinit area is 256 bytes");

extern noinit_t g_noinit;

/* Validates the area; on a bad magic/CRC (e.g. after power-on) it zeroes it and sets the magic. */
int  noinit_check(void);       /* returns 1 if it was valid */
void noinit_commit(void);      /* recompute the CRC after a change */

#endif
