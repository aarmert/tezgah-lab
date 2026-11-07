# firmware/boot_c0: bootloader stress-test firmware for the NUCLEO-C031C6

The C0 port of [`firmware/boot`](../boot/README.md) (the F407 reference). The contract is
[`docs/stress/SPEC.md`](../../docs/stress/SPEC.md) with the overrides of
[`SPEC-C031C6.md`](../../docs/stress/SPEC-C031C6.md) and the two-UART rule of
[`SPEC-ESP32.md`](../../docs/stress/SPEC-ESP32.md) §1. Protocol, meta log, boot decision, app
runtime and fault hook semantics are the F407's. This file only describes what is different.

Bare-metal C, no HAL. STM32C031C6 (Cortex-M0+, ARMv6-M), HSI48 at 48 MHz, 32 KB flash, 12 KB RAM.

**Hardware status (2026-11-07):** running on a NUCLEO-C031C6. Verified on the chip: boot at 48 MHz,
both UARTs, all app commands, v1<->v2 updates, `FAULT TRACE` timings (erase 88.1 ms, DATA 2.12 ms,
HDR 2.83 ms, META 0.36 ms), a fault reset in the middle of a slot erase, overflow-counted delays
(>65 ms), IWDG rollback after 3 attempts (`reset=IWDG,PIN`), option bytes 0xFFFFFEAA, UID at
0x1FFF7550, and the naive BL jumping into a half-written slot. Items in "Unverified" below that
these runs did not touch are still open.

## Build

```
python firmware/boot_c0/build_boot.py --json               # everything -> firmware/boot_c0/build/
python -m unittest discover -s firmware/boot_c0/tests -v   # host checks, no hardware
```

Same JSON fields as `firmware/boot/build_boot.py` (`ok`, `summary`, `error_type`,
`likely_causes`, `sizes`, plus `target: "c031"`). Flags: `-mcpu=cortex-m0plus -mthumb
-mno-unaligned-access -Os -Werror`, linked with `-lgcc`. The build fails if a BL is larger than
12 KB or an app payload is larger than 8192 - 256 B.

`wrong_board_{A,B}.img` contains the real F407 v2 app, so the build needs
`firmware/boot/build/app_v2_{A,B}.bin`. If they are missing, `build_boot.py` runs the F407 build
first (no F407 source is changed).

Outputs (`firmware/boot_c0/build/`): the same names as the F407 build.

| File | What |
|---|---|
| `bl_ab`, `bl_ab_hooks`, `bl_naive`, `bl_naive_hooks` (`.elf/.bin/.map`) | BLs at 0x08000000 |
| `app_<variant>_<A\|B>[_nohooks]` | apps at `slot_base + 0x100` (v1, v2, v2crash, v2hang, v2noconfirm, old) |
| `images/*.img` | every image of SPEC 2 in the C0 format (256 B header) |
| `images/golden_{ab,naive}[_nohooks].{bin,hex}`, `golden_ab_metafull.*` | 0x08000000..0x08007FFF (all 32 KB) |
| `images/release/` | the image set from the release apps, release goldens under the usual names |
| `images/manifest.json` | the F407 keys plus `"target": "c031"` in every entry |

## Layout (SPEC-C031C6 1)

| Pages | Address | Size | Use |
|---|---|---|---|
| 0-5 | 0x08000000 | 12 KB | BL |
| 6 | 0x08003000 | 2 KB | META0 (64 records) |
| 7 | 0x08003800 | 2 KB | META1 (64 records) |
| 8-11 | 0x08004000 | 8 KB | SLOT A: 256 B header + payload at 0x08004100 |
| 12-15 | 0x08006000 | 8 KB | SLOT B: 256 B header + payload at 0x08006100 |

RAM: `0x20000000` `.noinit` (256 B), `0x20000100` RAM vector table (48 words, 256-aligned), then
`.ramfunc`, `.data`, `.bss`, stack from `0x20003000` down (the link script keeps at least 2 KB).
All addresses live in `layout.h`; `mkimage.py` parses it.

Image header: 256 B, fields 0..63 as SPEC 2, 0xFF up to 251, `header_crc32` at 252 over 0..251,
`board_id = 0xC031C600`, `payload_size` a multiple of 8 (mkimage pads with 0xFF), at most 7936.
`golden_ab_metafull` holds 63 of META0's 64 records.

| Image | C0 specifics |
|---|---|
| `wrong_board_X` | the F407 v2 app (`firmware/boot/build/app_v2_X.bin`, padded to 8), header fields from the F407 (board `0xF407D15C`, link `0x08008200`/`0x0800C200`), **in the C0 header format**: a raw 512 B F407 header would be `ERR BAD_HDR` here (CRC at another offset), not `ERR BOARD` |
| `oversize_X` | payload_size 9000 |
| `truncated_X` | half the payload, rounded down to 8 |

## Source files

| File | What |
|---|---|
| `layout.h`, `noinit.h`, `common.h` | addresses, noinit struct (+ `ftimer`), RM0490 registers, prototypes |
| `startup_boot.c` | 48-entry vector table (TIM3 = IRQ 16, TIM14 = IRQ 19), `.data`/`.ramfunc` copy, RAM vector table |
| `hw.c` | 48 MHz clock, GPIO, USART1 + USART2, RX rings + per-UART line buffers, TIM14 time base, IWDG, strings |
| `flash.c` | page erase and double-word program; busy loops in `.ramfunc` |
| `crc32.c`, `sha256.c`, `b64.c` | **byte-identical copies** of `firmware/boot` (a test enforces it) |
| `image.c`, `meta.c` | header/slot validation and the meta log (pages instead of sectors) |
| `fault.c` | fault hook on TIM3 (16 bit) |
| `bl.c`, `bl_update.c`, `bl.h` | boot decision (unchanged) and update protocol (C0 line/alignment rules) |
| `app.c`, `app_rt.c`, `app_rt.h` | app variants and runtime (two UARTs, `OPTR` command) |

Shared code: crc32/sha256/b64 are copied, not included from `../boot`, because their
`#include "common.h"` always resolves next to the source file (the F407 header). The rest
(image/meta/update/boot decision/app runtime) is the F407 code with the C0 changes made in place;
a later `firmware/boot_common/` refactor can merge them.

## What differs from the F407 build

**Clock.** Reset default is HSISYS = HSI48/4 = 12 MHz. `hw_init` sets FLASH_ACR LATENCY = 1 and
waits for it to read back, *then* sets RCC_CR.HSIDIV = /1 (48 MHz). SW stays HSISYS, AHB/APB /1.
The app inherits this; `hw_init` is idempotent and does not touch a USART that is already set up.

**Two UARTs (SPEC-ESP32 1).** USART1 PA9/PA10 (update channel, ESP32) and USART2 PA2/PA3 (VCP
witness), AF1, 115200 (BRR 417), pull-ups on PA10 and PA3. Every output byte goes to USART1 and then
USART2, so both see the same lines in the same order. Input: whenever the firmware polls or waits
for TXE, received bytes go into a 64-byte ring per UART; `line_poll()` assembles one line per UART
(`\r` or `\n` ends a line, empty lines are skipped). A line that is longer than the limit, holds a
NUL, or lost a byte (ring full, overrun, framing/noise/parity error: the byte becomes a NUL) is
answered `ERR LINE` (BL) or `ERR unknown command` (app). Limits: 400 chars for every BL line, 96 in
the app. Replies always go to both UARTs.

**Update protocol (SPEC-C031C6 4).** One 400-char limit (no BEGIN exception: a BEGIN is 354
chars). DATA: `off` and the decoded length must be multiples of 8, otherwise `ERR SEQ <expected>`;
past the image size → `ERR SIZE`. The check order is otherwise SPEC 4's. The 3 s inactivity timer
restarts after every line that was **not** answered `ERR LINE` (SPEC-ESP32 1 "valid command"); the
F407 restarts it on any received byte, so a stream of garbage would keep it in update mode.

**Flash driver (RM0490).** Unlock with KEY1/KEY2. Each operation: wait until BSY1 and CFGBSY are
clear, clear all error flags + EOP, then
- page erase: `PER | PNB(p)`, then `STRT`; the 4 pages of a slot are erased in one RAM call;
- program: `PG`, then per double-word word 0 and word 1 with interrupts masked for the two stores
  (the second store starts the program), then wait for BSY1/CFGBSY, check the error flags.

Errors checked: OPERR, PROGERR, WRPERR, PGAERR, SIZERR, PGSERR, MISSERR, FASTERR, RDERR. EOP is
not used as the success signal (it is only set with EOPIE); the callers read the data back. After
every operation the instruction cache is reset (ICEN off, ICRST on/off, ICEN restored). A
double-word that is not erased is never programmed (`flash_program` refuses it, `-3`). Guard: only
pages 6..15 can be erased, only 0x08003000..0x08007FFF programmed. The driver has no MER1, no
OPTKEYR/OPTSTRT/OBL_LAUNCH and never writes FLASH_OPTR (the tests check the source).

**Fault timer (SPEC-C031C6 6).** TIM3 at 1 MHz (PSC 47), ARR = 0xFFFF, free running. For a delay
of `T` ticks (`T = max(delay_us, 2)`, so 1 behaves like 2 as on the F407) the counter is preset to
`start = (0x8000 - T) mod 65536` and `wraps_left = (start + T) >> 16` goes to
`noinit.ftimer` (after the CRC word, like `expiry`). The RAM ISR decrements `wraps_left` on every
update event and fires (expiry record, SYSRESETREQ) on the CC1 match at 0x8000 once it is 0.
Update and compare events are 32 ms apart, so they are never seen together. Keeping the state in
noinit (not `.bss`) lets the app's identical ISR take over a timer that is still pending at the
jump. A unit test models the arithmetic for delays from 1 µs to 10 s. ERASE uses one fault point
per slot erase with index = the slot's first page (8 = A, 12 = B); ROTATE uses the meta page
(6 = META0, 7 = META1). The other phases/indices are the F407's.

**Time base.** TIM14 at 1 MHz, free running, overflow count in a RAM ISR (priority 1, below TIM3
at 0). `time_us()` reads count + CNT with interrupts masked and corrects for a pending overflow.
Used for T lines, `OK ERASED <us>`, timeouts and the IWDG wait (there is no DWT on the M0+). The BL
stops TIM14 at the jump and the app restarts it.

**ARMv6-M.** No unaligned access: header fields are copied byte-wise (`mem_copy`, `rd32le`), the
chunk and header buffers are `uint32_t` arrays, flash addresses are page/slot aligned. A test lists
every cast to a 32-bit pointer type and fails on any cast not reviewed as aligned. Decimal output
uses subtraction (no divide); `-lgcc` is linked for the remaining helpers (`__aeabi_llsr` in SHA).
NVIC priorities are written as words (byte access to NVIC_IPR is not allowed on ARMv6-M).
HardFault has no CFSR/HFSR: `HARDFAULT pc=.. lr=.. cfsr=0x00000000 hfsr=0x00000000`.

**Reset cause.** From RCC_CSR2 (cleared with RMVF), printed in the SPEC 3 order
`LPWR,WWDG,IWDG,SFT,POR,PIN,OBL`: the C0 has no separate BOR flag (POR = PWRRSTF, which covers
POR/PDR/BOR), and OBLRSTF takes the last place. PWRRSTF or OBLRSTF clears the noinit expiry record.

**App.** As the F407 app, plus `OPTR` → `OPTR 0x........` (FLASH_OPTR, read only) so the
option byte check of SPEC-C031C6 7 needs no `bench.py mem` (bench.py's read regions are the
F407's). `UID` reads 0x1FFF7550. LED: LD4 = PA5 (heartbeat).

## Unverified (from memory of RM0490 / the datasheet / UM2953, not checked on a chip)

Confirmed by `firmware/wiretest_c031/` on the real board: reset clock 12 MHz, RCC base and
IOPENR/APBENR1/APBENR2/CSR2 offsets, CSR2 RMVF (23), PINRSTF (26), PWRRSTF (27), GPIOA, USART1/2
base and register offsets, AF1 on PA2/3/9/10. Everything below is not confirmed:

1. RCC_CR: HSIDIV = bits 13:11, HSIRDY = bit 10. FLASH_ACR: LATENCY = bits 2:0, ICEN = 9, ICRST = 11.
2. FLASH (0x40022000): KEYR +0x08, SR +0x10, CR +0x14, OPTR +0x20; SR bits EOP 0, OPERR 1, PROGERR 3,
   WRPERR 4, PGAERR 5, SIZERR 6, PGSERR 7, MISSERR 8, FASTERR 9, RDERR 14, BSY1 16, CFGBSY 18; CR bits
   PG 0, PER 1, PNB from bit 3, STRT 16, LOCK 31. EOP only with EOPIE.
3. TIM3 at 0x40000400 (IRQ 16, APBENR1 bit 1) and TIM14 at 0x40002000 (IRQ 19, APBENR2 bit 15);
   timer clock = PCLK (48 MHz) with APB /1; 32 external IRQs (48 vectors).
4. CSR2 SFTRSTF 28, IWDGRSTF 29, WWDGRSTF 30, LPWRRSTF 31, OBLRSTF 25; whether a software or IWDG
   reset also sets PINRSTF (it does on the F4; the host only looks for `IWDG`).
5. UID at 0x1FFF7550. LD4 on PA5. LSI ~32 kHz (IWDG ~2 s with /32, reload 2000).
6. The C031 USART has no FIFO in use here (FIFOEN stays 0), so one byte of hardware buffering: a
   host that pipelines during a flash operation gets an overrun (→ bad line), as specified.
7. Whether writing TIM3_CNT equal to CCR1 raises CC1IF at once: harmless either way (the ISR ignores
   a match while `wraps_left` > 0, and with `wraps_left` = 0 the start is always below 0x8000).
8. Option bytes assumed at factory defaults (IWDG_SW = 1, NRST_MODE = reset pin, boot from main
   flash): check with the app's `OPTR` command before the first stress run.

## Suggested first bring-up

1. Flash `images/golden_ab_nohooks.hex` (release BL + release v1). On the VCP: `BL v0.1 design=ab
   target=c031 slotA=ok:1.0.0 slotB=empty active=A state=confirmed attempts=0 seq=1 erases=0,0
   reset=...`, `BL jump A`, `BOOT app 1.0.0 slot=A trial=0 reset=...`. Garbage means the 48 MHz
   clock or BRR is wrong.
2. `PING`, `VER`, `TICK` twice 1 s apart (≈1000 ms apart → SysTick at 48 MHz), `UID`, `OPTR`.
   Then the same commands from the ESP32 side (USART1): the replies must appear on both ports.
3. `UPD` → `BL update`; a full v2 update → `OK COMMIT`, `BL jump B trial`, `APP confirmed`.
4. Flash `images/golden_ab.hex` (hooks). `FAULT TRACE ON`, then an update: check the T lines
   (ERASE 8/12 ≈ 90 ms, DATA ≈ 2 ms, HDR ≈ 3 ms, META ≈ 0.4 ms).
5. `FAULT ARM 0 ERASE -1 40000` then an update: the board must reset in the middle of the slot
   erase (proves the RAM ISR runs while the flash is busy) and print `BL fault_fired 1 ERASE 12 40000`.
   Repeat with a delay > 65535 (e.g. 150000 across DATA) to exercise the overflow counting.
6. v2noconfirm → rollback after 3 IWDG resets (`reset=` contains `IWDG`).
7. ~20 updates in a row: META rotates (`erases=` changes) at every ~16-20 updates.
