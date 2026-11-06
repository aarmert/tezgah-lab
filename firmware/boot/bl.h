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

/* Runs the update protocol (SPEC section 4). Returns when the BL should boot normally (after
 * "BL timeout" or "OK ABORT"). A successful UPD END never returns (OK COMMIT, then reset). */
void bl_update_mode(int mode);

#endif
