#ifndef BL_H
#define BL_H
#include "common.h"

#ifdef DESIGN_NAIVE
#define BL_DESIGN "naive"
#else
#define BL_DESIGN "ab"
extern meta_state_t g_meta;
#endif

enum { UPD_NORMAL = 0, UPD_RESUME, UPD_NOFW };

/* SPEC-C031C6 4: one limit of 400 chars for every update-mode line (UPD BEGIN is 10 + 344). */
#define UPD_LINE_MAX 400u

/* Runs the update protocol (SPEC section 4). Returns when the BL should boot normally (after
 * "BL timeout" or "OK ABORT"). A successful UPD END never returns (OK COMMIT, then reset). */
void bl_update_mode(int mode);

#endif
