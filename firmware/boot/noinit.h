/*
 * Shared .noinit RAM area at 0x20000000 (256 B), SPEC section 6.3.
 * The BL and every app reserve it in their link scripts and never zero it.
 * The part before `crc32` is valid when magic == NOINIT_MAGIC and crc32 matches the bytes before
 * it. `expiry` comes after the CRC word and is NOT covered by it: the TIM2 fault ISR writes it
 * from RAM right before SYSRESETREQ and must not call crc32() (which lives in flash). It carries
 * its own magic and check word.
 * The host never reads this struct; it is documented here only.
 */
#ifndef NOINIT_H
#define NOINIT_H
#include <stdint.h>
#include "layout.h"

#define NOINIT_MAGIC 0x4E4F494Eu   /* "NOIN" */
#define EXPIRY_MAGIC 0x44505845u   /* "EXPD" */
#define EXPIRY_SALT  0xA5C3F00Fu

typedef struct {
    uint32_t armed;       /* 1 = armed */
    uint32_t phase;       /* enum fault_phase */
    int32_t  index;       /* -1 = any */
    uint32_t delay_us;
} noinit_fault_t;

typedef struct {
    uint32_t count;
    uint32_t phase;
    int32_t  index;
    uint32_t delay_us;
} noinit_report_t;

/* Written by the TIM2 ISR (or the delay-0 path) just before the fault reset. */
typedef struct {
    uint32_t magic;       /* EXPIRY_MAGIC */
    uint32_t count;       /* fault timers that expired since the last BL report */
    uint32_t seq;         /* pending.seq of the last one that expired */
    uint32_t phase;       /* the last one that expired */
    int32_t  index;
    uint32_t delay_us;
    uint32_t check;       /* expiry_check() of the fields above */
} noinit_expiry_t;

typedef struct {
    uint32_t magic;
    uint32_t enter_update;        /* app UPD command -> BL enters update mode */
    uint32_t update_in_progress;  /* set at BEGIN, cleared at COMMIT/ABORT/timeout/END error */
    noinit_fault_t fault[2];
    struct {                      /* the fault timer that was started last */
        uint32_t active;          /* started, not yet seen expired or cancelled */
        uint32_t seq;             /* incremented at every start */
        uint32_t phase;
        int32_t  index;
        uint32_t delay_us;
    } pending;
    noinit_report_t cancelled;    /* started timers that never expired (reset by another cause,
                                     FAULT CLEAR); count since the last BL report + the last one */
    uint32_t trace;               /* 1 = print "T <PHASE> <idx> <us>" lines */
    uint32_t reset_csr;           /* RCC_CSR seen by the BL (it clears the flags; the app prints this) */
    uint32_t crc32;               /* crc32 over all bytes before this field */
    noinit_expiry_t expiry;       /* NOT covered by crc32 (see above) */
} noinit_t;

_Static_assert(sizeof(noinit_t) <= NOINIT_SIZE, "noinit area is 256 bytes");

extern noinit_t g_noinit;

static inline __attribute__((always_inline)) uint32_t expiry_check(uint32_t count, uint32_t seq,
                                                                  uint32_t phase, int32_t index,
                                                                  uint32_t delay_us) {
    return EXPIRY_MAGIC ^ EXPIRY_SALT ^ count ^ (seq << 1) ^ (phase << 8) ^ (uint32_t)index ^ ~delay_us;
}

static inline __attribute__((always_inline)) int expiry_valid(const volatile noinit_expiry_t *x) {
    return x->magic == EXPIRY_MAGIC &&
           x->check == expiry_check(x->count, x->seq, x->phase, x->index, x->delay_us);
}

/* Validates the CRC part; on a bad magic/CRC (e.g. after power-on) it zeroes that part and sets
 * the magic. The expiry record is validated on its own (expiry_valid) and is cleared on a
 * power-on/brown-out reset. */
int  noinit_check(void);       /* returns 1 if it was valid */
void noinit_commit(void);      /* recompute the CRC after a change */

#endif
