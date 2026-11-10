# firmware/boot: bootloader stress-test firmware

Firmware half of the bootloader stress test. The binding contract is
[`docs/stress/SPEC.md`](../../docs/stress/SPEC.md). The host tool (`stress/`) is built against
the same spec. Bare-metal C, no HAL, STM32F407G-DISC1, HSI 16 MHz, USART2 115200 8N1.

The original test firmware (`firmware/main.c`, `build.py`, `plans/smoke.toml`) is unchanged and
does not depend on this folder.

## Build

```
python firmware/boot/build_boot.py --json          # everything -> firmware/boot/build/
python -m unittest discover -s firmware/boot/tests -v   # host checks, no hardware
```

`build_boot.py` returns the same JSON fields as `firmware/build.py` (`ok`, `summary`, `error_type`,
`likely_causes`) plus per-target `sizes`. Warnings are errors (`-Werror`, and any linker warning
fails the build). It fails if a BL is larger than 16 KB or an app payload is larger than 16384-512 B.

Outputs in `firmware/boot/build/`:

| File | What |
|---|---|
| `bl_ab.elf/.bin`, `bl_ab_hooks.*` | A/B bootloader, release and `-DFAULT_HOOKS` |
| `bl_naive.*`, `bl_naive_hooks.*` | naive bootloader, release and hooks |
| `app_<variant>_<A\|B>[_nohooks].elf/.bin` | apps linked at `slot_base + 0x200` (variants: v1, v2, v2crash, v2hang, v2noconfirm, old), hooks and release (`_nohooks`) |
| `images/*.img` | every image of SPEC section 2 (header + payload) |
| `images/golden_ab.bin/.hex`, `golden_naive.*` | 0x08000000..0x0801FFFF, **hooks** BL, META0 initial record, v1 in A |
| `images/golden_*_nohooks.*` | the same with the release BL and the release v1 app |
| `images/golden_ab_metafull.*` | `golden_ab` with META0 one record short of full (511/512): the next update rotates (`--meta-near-full`) |
| `images/release/` | the whole image set from the release apps (no `FAULT_HOOKS`), same names, own manifest |
| `images/manifest.json` | name → {sha256, size, payload_sha256, version, slot, variant, security_counter, payload_size, link_addr, expected_err, ...} |

Use the `.hex` golden files with OpenOCD `program`: a raw `.bin` has no address. The hex includes
the erased (0xFF) areas, so slot B and META1 really get erased.

`mkimage.py` can also wrap a single payload:
`python firmware/boot/mkimage.py --payload x.bin --version 1.2.3 --sc 1 --slot B -o x.img`.

## Layout

| Sector | Address | Size | Use |
|---|---|---|---|
| S0 | 0x08000000 | 16 KB | BL |
| S1 | 0x08004000 | 16 KB | META0 (512 records) |
| S2 | 0x08008000 | 16 KB | SLOT A: header 512 B + payload at 0x08008200 |
| S3 | 0x0800C000 | 16 KB | SLOT B: header 512 B + payload at 0x0800C200 |
| S4 | 0x08010000 | 64 KB | META1 (2048 records) |

RAM (identical in the BL and every app, see `link.ld.in`): `0x20000000` `.noinit` (256 B, never
zeroed, `noinit.h`), `0x20000200` RAM vector table (512-aligned), then `.ramfunc`, `.data`,
`.bss`, and the stack from `0x20020000` down.

All addresses live in `layout.h`. `mkimage.py` parses `layout.h`, so the two cannot drift apart.

## Source files

| File | What |
|---|---|
| `layout.h`, `noinit.h`, `common.h` | addresses, noinit struct, registers and shared prototypes |
| `startup_boot.c` | 98-entry vector table, copies `.data` and `.ramfunc`, zeroes `.bss`, copies the vector table to RAM, sets VTOR, `cpsie i` |
| `hw.c` | clocks/GPIO/USART2, DWT timing, line reader with timeout, IWDG, reset, string helpers |
| `flash.c` | sector erase and word program. The busy-wait loops are in `.ramfunc` |
| `crc32.c`, `sha256.c`, `b64.c` | CRC-32 (zlib-compatible, RAM table), SHA-256, base64 decoder |
| `image.c` | header and slot validation (SPEC section 2) |
| `meta.c` | meta log: load, append, rotation (SPEC 4.1) |
| `fault.c` | fault hook: fault points, TIM2 one-shot, RAM ISR, FAULT commands, TRACE (SPEC 6) |
| `bl.c`, `bl_update.c`, `bl.h` | boot decision for both designs, and the update protocol |
| `app.c`, `app_rt.c`, `app_rt.h` | app variants and the shared app runtime |

## Flash operations run from RAM (SPEC 5)

On the F407, a fetch from flash stalls while the flash is erasing or programming. These live in
`.ramfunc` (copied to RAM by the startup):

- `ram_erase`/`ram_program` (`flash.c`): they hold the whole erase, or the whole word loop of a
  chunk, including the BSY polling.
- `TIM2_IRQHandler` (`fault.c`) does SYSRESETREQ.
- the app's `SysTick_Handler`.

VTOR points to a RAM copy of the vector table **for the whole run time**, from the startup code on,
not just during flash operations. That covers "VTOR in RAM during flash operations" and needs no
switching. The only interrupts ever enabled are TIM2 (hooks builds) and SysTick (apps). Both
handlers are in RAM, so neither one fetches from flash. Callers reach the RAM functions with
`long_call` (`blx` through a register). `tests/test_mkimage.py` disassembles `.ramfunc` and fails if
it finds a branch or a literal that points into flash.

## The A/B bootloader (`design=ab`)

Boot (SPEC 4.2): status line (`... design=ab[ hooks] target=f407 ...`), the fault report of hooks
builds (`BL fault_fired ...` for timers that expired, `BL fault_cancelled ...` for timers that were
started but never expired), then the decision:

1. No valid meta record: if a slot is valid (A first), append CONFIRMED (min_sc = that image's
   counter). If neither slot is valid → `BL nofw` (update mode with no timeout).
2. noinit `enter_update` → `BL update`. noinit `update_in_progress` → `BL resume`. Both enter
   update mode with a 3 s inactivity timeout (`BL timeout`), then continue with step 3. `UPD ABORT`
   also leaves update mode this way (no reset, no new status line). In update mode every line the BL
   does not recognise gets `ERR LINE`.
3. Trial pending: if attempts ≥ 3 or the trial slot is invalid → ROLLBACK record (attempts kept as
   evidence), `BL rollback T->X`. Otherwise ATTEMPT (attempts+1), start the IWDG (~2 s), `BL jump T trial`.
4. Active valid → `BL jump X`. Otherwise, if the other slot is valid → REPAIR, `BL repair active=Y`,
   `BL jump Y`. Otherwise → `BL nofw`.

"Valid" = header magic/version/CRC, board, size ≤ 15872 and a multiple of 4, link_addr = this
slot's payload address, payload CRC32 (checked on every boot).

The update (in update mode, SPEC 4) always goes to the inactive slot. With no meta record, it goes to A.
Commit order: payload → END verifies CRC32 and SHA-256 by reading the flash back → header
(written in ascending order, so its CRC word at 508 is written last) → TRIAL record (CRC word last)
→ clear `update_in_progress` → `OK COMMIT` → reset.

Meta log: a record goes after the last non-erased record slot of the sector that holds the latest
record, so a half-written record is skipped and never overwritten. When that sector is full: erase
the other sector (fault point `ROTATE <sector>`) and write the new record, which is a full state
snapshot, at its start with that sector's erase count + 1. The old sector stays intact until the
next rotation. A reset in the middle of a rotation leaves the old latest record in place, and the
next append rotates again.

## The naive bootloader (`design=naive`): deliberately unsafe

This is a negative control. If the stress test cannot break it, the test measures nothing.

- BEGIN erases **slot A in place**: the image that is running right now. It checks only the header
  magic/CRC, and the payload size so writes stay inside slot A. No board, link or version check.
- DATA writes straight into slot A.
- END writes the header with **no check at all**: no SHORT, no CRC, no SHA, no meta.
- At boot it jumps to A when payload word 0 looks like a RAM stack pointer
  (`0x20000000 < sp <= 0x20020000`). It never checks the CRC, so a half-written or interrupted image
  can run (WRONG), crash or hang (DEAD).
- Its status line reads `ok:<ver>` when the header magic is present (it trusts the header), and prints
  `slotB=- state=none attempts=0 seq=0 erases=0,0`.
- No meta log, no trial, no IWDG, no rollback.

## Apps

`BOOT app <ver>[-suffix] slot=X trial=0|1 reset=..`, where `trial=1` means the meta trial is this
app's slot. Commands: PING, VER (plain `x.y.z`), TICK, UID, RESET, CRASH, UPD, STATE, FAULT, and
unknown → `ERR unknown command`. Commands are not case sensitive (the line is upper-cased).

- `STATE active=X trial=0|1 confirmed=0|1 seq=N`, read fresh from the meta log: `trial=1` means a
  trial is pending (meta trial ≠ none). `confirmed=1` means no trial is pending and active == this
  app's slot.
- Confirm: a trial app appends CONFIRM (active = own slot, trial none, attempts 0,
  min_sc = max(min_sc, own header's counter)) after 1 s and prints `APP confirmed`. If the append
  fails, it prints `APP error META` and retries every second until the deadline.
- Trial deadline: unconfirmed after 5 s → it stops feeding the IWDG.
- The IWDG is fed from the main loop always (reloading a stopped IWDG is harmless and does not start it).
- HardFault (CRASH, v2crash) → `HARDFAULT pc=.. lr=.. cfsr=.. hfsr=..`, then reset. This differs from
  `firmware/main.c`, which halts: a trial crash has to count as a failed attempt.
- Every app is built twice: with `FAULT_HOOKS` (`app_<variant>_<slot>`, the CONFIRM fault point
  lives in the app; `images/`) and without (`app_<variant>_<slot>_nohooks`; `images/release/`, and
  the v1 in `golden_*_nohooks`). A release board (`golden_*_nohooks`) then has no FAULT command
  anywhere, matching the BL status line without `hooks`.

## Fault hook (hooks builds)

`fault_point()` (see SPEC 6.2 for the phases) disarms the matching entry in noinit first, records
the start (`noinit.pending`, new seq) and starts TIM2 (1 MHz one-shot). `delay_us = 0` resets on the
spot, `delay_us = 1` behaves like 2 (ARR must be ≥ 1). While a fault timer is pending, other entries
stay armed, so entry 1 survives into the next boot. A pending timer keeps running across phases and
across the jump into the app: the BL jumps with interrupts masked, and the app unmasks them once its
own RAM ISR and RAM vector table exist.

"Fired" means the timer **expired**: the RAM ISR writes `noinit.expiry` (after the CRC word, own
magic + check word, no flash calls) right before SYSRESETREQ. A timer that was started but never
expired (`OK COMMIT` or another reset came first, or `FAULT CLEAR`) is "cancelled". At entry, before
the BOOT fault point, the BL resolves the last start (expiry seq == pending seq → fired, else
cancelled) and snapshots both counts; after the status line it prints `BL fault_fired ...` /
`BL fault_cancelled ...` and subtracts only what it printed, so a BOOT fault that resets at once
loses nothing.
`FAULT ARM ... [reset]`: `reset` is the fault action (the default and the only one).

## Untested on hardware

This was built and checked statically (unit tests, disassembly). It has not run on the board. The
first hardware session should check, in this order: the status line on `golden_ab.hex`; `UPD`, then a
full v2 update; `FAULT TRACE ON` timings; a fault in the middle of an ERASE (proves the RAM ISR fires
while the flash is busy); v2noconfirm rollback (IWDG); and many updates in a row to force a meta
rotation (META0 holds 512 records, about 3-4 per update).
