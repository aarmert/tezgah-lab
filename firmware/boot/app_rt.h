#ifndef APP_RT_H
#define APP_RT_H
#include "common.h"

#ifndef APP_SLOT
#error "APP_SLOT (0=A, 1=B) must be defined by the build"
#endif
#ifndef APP_VERSION
#error "APP_VERSION (\"x.y.z\") must be defined by the build"
#endif

void rt_init(void);
void rt_banner(const char *version_with_suffix);
/* Main loop: IWDG feeding, confirm logic, trial deadline, commands. Never returns. */
__attribute__((noreturn)) void rt_run(int may_confirm);

#endif
