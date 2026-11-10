# Bootloader stress test: NUCLEO-C031C6 target (addendum to SPEC.md, v0.1.1)

This file defines the same test system for a second target, the **NUCLEO-C031C6** board
(STM32C031C6T6). Everything in `SPEC.md` applies unchanged **except what this file overrides**.
Section numbers below refer to `SPEC.md`. When the two disagree, this file wins for the C0 target
and `SPEC.md` wins for the F407 target.

Unchanged and shared: the line protocol (§4 commands, stop-and-wait, error codes, idempotent DATA),
the commit order, the meta record format and kinds (§4.1), the boot decision (§4.2), the app
runtime rules (§4.3), anti-rollback (§4.4), the FAULT commands and phases (§6.1, §6.2), the outcome
classification (§7), the test list (§8), the seeds and the CSV/summary format (§9). One host tool
and one classifier serve both boards. Results go in separate run folders and are reported
separately (F407 vs C031).

Facts marked **(verify)** come from memory of ST documents and must be checked in RM0490 (STM32C0
reference manual), the STM32C031 datasheet, or the NUCLEO-C031C6 user manual before the code relies
on them. **(confirmed)** marks a fact the real board showed on 2026-11-07 (wiring test
`firmware/wiretest_c031/` and the stress runs); details in `firmware/boot_c0/README.md`.

## 0. Hardware

| | F407 Discovery (SPEC.md) | NUCLEO-C031C6 (this file) |
|---|---|---|
| Core | Cortex-M4F, Thumb-2 | **Cortex-M0+**, ARMv6-M Thumb |
| Flash | 1 MB, sectors 16/64/128 KB | **32 KB, 16 pages of 2 KB** |
| Program unit | 32-bit word, ~16 µs | **64-bit double-word, ~85 µs** |
| Erase unit | sector, 0.25-2 s | **page, 22.0 ms measured; a 4-page slot 88.1 ms (confirmed)** |
| ECC | no | **no** (a half-written double-word leaves silent wrong bits; only CRC/SHA catches it) |
| RAM | 128 KB SRAM | **12 KB SRAM** |
| Clock | HSI 16 MHz | **HSI48 at 48 MHz** (reset default is HSISYS = 12 MHz, HSIDIV = /4) |
| Debugger | ST-Link/V2 on board | **ST-LINK/V2-1 on board** (this bench's board) |
| Serial | USART2 via an external USB-TTL | **USART2 PA2/PA3 via the ST-LINK virtual COM port** (`/dev/ttyACM*`) |
| User LED | PD12-PD15 | **LD4 = PA5 (verify)** |
| Button | PA0 | **B1 = PC13 (verify)** |
| SWD | PA13/PA14 | PA13/PA14 (still forbidden for any other use) |

Phase 1 (no ESP32) needs **only the single Nucleo USB cable**: the ST-LINK gives SWD (recovery and
the flash-reading witness) and the VCP (the test serial line). The ST-LINK MCU stays powered while
the target resets, so the VCP port does not disappear on a reset.

Phase 2 (ESP32) uses **USART1 on PA9 (TX) / PA10 (RX)**, which is not on the VCP, plus an
open-drain ESP32 GPIO to NRST, plus GND **(confirmed: PA9/PA10 are separate pins on this LQFP48 board (MB1717B). On the bench
PA9 (D8) and PA10 (D2) are wired on the right morpho header CN10, NRST and GND on the left morpho header
CN7, the same nets as the Arduino CN6 pins (photos in `docs/egitim/foto/`). On small C0 packages
PA9/PA10 are remapped onto PA11/PA12 through SYSCFG)**. The ST-LINK also
drives NRST; both drivers are open-drain, so they can share the line.

## 1. Flash layout (overrides §1)

| Pages | Address | Size | Use |
|---|---|---|---|
| 0-5 | 0x08000000 | 12 KB | Bootloader (BL) |
| 6 | 0x08003000 | 2 KB | META0 |
| 7 | 0x08003800 | 2 KB | META1 |
| 8-11 | 0x08004000 | 8 KB | SLOT A |
| 12-15 | 0x08006000 | 8 KB | SLOT B |

Reasoning:
- The F4 builds are 7.0-8.3 KB for the BL and about 5.1 KB for an app. ARMv6-M code is usually
  10-25% larger (no Thumb-2, no hardware divide, no unaligned access). 12 KB leaves room for the
  `hooks` BL; 8 KB slots leave about 7.7 KB for an app.
- The build fails if the BL exceeds 12 KB or an app payload exceeds 8192 - 256 bytes.
- Erasing one slot is 4 pages, about 90 ms. That is ~3-5x faster than the F407's 16 KB sector, so
  the 3-5 s per trial in the original plan becomes realistic.
- Unlike the F407, META0 and META1 are now the same size.

RAM: the shared `.noinit` area stays at 0x20000000, 256 bytes. RAM ends at 0x20003000.

Layout constants live in `firmware/boot_c0/layout.h` and `stress/layout_c031.py`, chosen through
the target name (section 10).

## 2. Image format (overrides parts of §2)

- **Header size 256 B instead of 512.** The C031 has about 32 external interrupts, so the vector
  table is 48 words (192 B). VTOR needs the table aligned to the next power of two, 256 B
  **(works on the board: the apps run with VTOR at `slot_base + 0x100`; IRQ count not checked in
  RM0490)**. Payload starts at `slot_base + 0x100`.
- `header_crc32` sits at offset **252** and covers bytes 0..251. The fields at offsets 0-63 are
  unchanged.
- `board_id = 0xC031C600`.
- `payload_size` must be a **multiple of 8**; mkimage pads the payload with 0xFF.
- Valid only if `payload_size ≤ 8192 - 256`.
- Image names in the table are the same, with two changes:
  - `wrong_board_{A,B}.img` is now literally **a real F407 image** (board_id `0xF407D15C`), so
    "another board's image" is tested with a real one. Expected error: `BOARD`. Precisely: the
    real F407 v2 app payload with the F407 board id and F407 link address, **in the C0 header
    format** (256 B, header_crc at 252). A literal F407 `.img` would answer `BAD_HDR`: its
    header_crc sits at 508, so the C0's CRC check (before the board check) fails first.
  - `oversize_{A,B}.img` has payload_size = 9000.
- `golden_ab.{bin,hex}` / `golden_naive.{bin,hex}` cover 0x08000000..0x08007FFF (all 32 KB).

## 3. Serial lines (small overrides to §3)

- The BL status line is the same, but reads `BL v0.1 design=... target=c031 ...` with an added
  `target=(f407|c031)` field right after `design=...(?: hooks)?`. The F407 firmware should add
  `target=f407` too, so one regex covers both. The host must accept the line without `target=`
  as f407 while the old builds are around.
- `erases=<n0>,<n1>` are the META0/META1 page erase counts.
- The ARMv6-M core has no CFSR/HFSR. To keep the host regex unchanged, the C0 app prints
  `HARDFAULT pc=0x........ lr=0x........ cfsr=0x00000000 hfsr=0x00000000`.
- `UID` reads the 96-bit unique ID at **0x1FFF7550 (confirmed: returns a plausible 24-hex-digit ID)**.

## 4. Update protocol (overrides parts of §4)

- **Line length:** a BEGIN line is now 10 + 344 base64 chars, so **one limit of 400 chars applies
  to every line**. The BEGIN exception from SPEC.md is not needed.
- DATA: `off` and the decoded length must be **multiples of 8**, otherwise `ERR SEQ <expected>`.
  Chunk size stays at 192 B (24 double-words).
- Timeouts: BEGIN reply 1 s (4 pages ≈ 90 ms), everything else 1 s. The BL's 3 s inactivity
  timeout is unchanged.
- **Commit order is unchanged:** payload → END check → header written last (its last double-word
  holds header_crc) → TRIAL meta record → `OK COMMIT` → reset.

### 4.1 Meta records on the C0
- The 32-byte record layout is unchanged. A record is 4 double-words. The double-word that holds
  `crc32` (bytes 24-31) is **programmed last**.
- A double-word can only be programmed when it is erased. Never re-program a non-erased
  double-word to clear bits, as the F4 code may do. The C0 sets PROGERR for that **(verify the
  exact rule in RM0490 "Flash program and erase operations")**.
- Each 2 KB page holds 64 records. An update uses about 3-4 records, so the log **rotates every
  16-20 updates** instead of every 130-170. ROTATE is therefore exercised often, and the reset
  sweep reaches it without a special campaign. A rotation erases one page (~22 ms).

## 5. Flash code runs from RAM (overrides §5)

The same rule applies: a CPU fetch from flash stalls while that flash is being erased or
programmed. So the erase/program routines, the fault-timer ISR and a RAM copy of the vector table
(VTOR pointed at RAM) all run from RAM. C0-specific points for the flash driver:
- Registers: FLASH base 0x40022000: ACR +0x00, KEYR +0x08, SR +0x10, CR +0x14, OPTR +0x20
  **(confirmed in practice: erase and program work and are read back; the error flags were never
  provoked)**. Unlock with the usual KEY1/KEY2 sequence.
- Before starting: wait until `BSY1` and `CFGBSY` are clear. Clear all error flags in SR (EOP,
  OPERR, PROGERR, WRPERR, PGAERR, SIZERR, PGSERR, MISSERR, FASTERR).
- Page erase: `PER` + `PNB` = page number, then `STRT`.
- Programming: set `PG`, write word 0, then word 1 of the double-word; the second write starts the
  operation. Wait for `BSY1` to clear, check EOP and the error flags. Interrupts must not run any
  code between the two writes that touches flash (do the pair inside the RAM routine).
- Wait states: set FLASH_ACR LATENCY = 1 **before** switching to 48 MHz. After erase or program,
  reset the instruction cache (`ICRST` with `ICEN` cleared, then set it again), so stale code or
  data is never read **(LATENCY confirmed in practice at 48 MHz; ICEN/ICRST still to verify)**.
- Guard in the driver: refuse to erase pages 0-5 (the BL) and anything outside 6-15. The driver
  must not contain the `MER1` (mass erase) bit or any `OPTKEYR` / `OPTSTRT` writes at all. The
  firmware test checks that, as the F4 test does.

### ARMv6-M rules (new)
- **No unaligned access.** A 32-bit load from an address that is not a multiple of 4 is a
  HardFault on the M0+. Header and meta parsing, base64 output buffers and CRC/SHA input must use
  aligned buffers or byte access. The build adds `-mno-unaligned-access`, and the code never casts
  a `uint8_t *` to `uint32_t *` unless the buffer is declared aligned.
- **No hardware divide.** `/` and `%` on 32-bit values call `__aeabi_uidiv`/`__aeabi_uidivmod`.
  The build currently uses `-nostdlib`, so either link `-lgcc` or avoid division (use shifts; the
  decimal print routine is the usual offender).
- `-mcpu=cortex-m0plus -mthumb`, no `-mfloat-abi`.
- **No DWT cycle counter** on the M0+. Use the timers in §6.

## 6. Fault hook (overrides the timer parts of §6)

The C031 has **no 32-bit TIM2**. Available: TIM1 (advanced, 16-bit), TIM3, TIM14, TIM16, TIM17
(16-bit) **(TIM3 and TIM14 confirmed; the others are not used)**.
- **Fault timer:** TIM3 at 1 MHz (PSC = 47 at 48 MHz). For a `delay_us` up to 65 535, use one
  compare. Longer delays count overflows in the ISR and arm the final compare in the last period.
  The ISR is in RAM and does SYSRESETREQ, which also drives NRST low while NRST_MODE is the default
  bidirectional reset pin **(confirmed: a software reset reads `reset=SFT,PIN`)**.
- **Trace timing (`T` lines):** TIM14 free-running at 1 MHz with a software overflow counter, in
  place of DWT. Resolution 1 µs. The interrupt latency at 48 MHz is about 0.3 µs, well below the
  10 µs sweep step.
- The phases, commands, noinit behaviour and the double-fault rules are unchanged.
- **ERASE:** one fault point for the whole slot erase (four pages), index = the slot's first page
  (8 for A, 12 for B), and one `T ERASE <first page> <us>` line for all four pages. The pages are
  erased in order, so a reset in page k leaves the earlier pages erased and the later ones intact.
  `ROTATE` uses the meta page number (6 or 7). The host sweeps `ERASE -1` over the whole slot erase.
- Expected phase durations (from the calibration run, these are only guesses): ERASE ~22 ms per
  page, DATA ~2 ms per chunk (24 × 85 µs), HDR ~2.7 ms (32 double-words), META ~0.35 ms
  (4 double-words), ROTATE ~22 ms. **Measured on the board (calibration 2026-11-07,
  `stress_runs/calibration_c031.json`):** ERASE 88.1 ms per 4-page slot, DATA 2.12 ms, HDR 2.83 ms,
  META 0.36 ms, ROTATE 22.0 ms, BOOT ~16 ms (ab) / ~10.9 ms (naive), CONFIRM ~2.1 ms; the whole update
  (`UPD BEGIN` → `OK COMMIT`) ~1.4 s.

## 7. Watchdog and reset cause
- IWDG: same start sequence as the F4. LSI is ~32 kHz; prescaler 32 and reload 2000 give about
  2 s **(seen: 3 IWDG resets in 6.1 s; the LSI frequency itself was not measured)**.
- The `reset=` field reads the reset flags from **RCC_CSR2** (PIN, POR/PWR, SFT, IWDG, WWDG, LPWR,
  OBL), then clears them with RMVF **(confirmed; IWDG and software resets also set PIN)**.
- **Option bytes:** never write them. The tests assume the factory defaults: software IWDG
  (`IWDG_SW = 1`), NRST as a reset pin, normal boot from main flash. Check these by **reading**
  FLASH_OPTR (0x40022020) once; if anything differs, stop and tell the human.

## 8. Tests (overrides §8 numbers only)

The test list and criteria are unchanged. On the C0:

| Test | Change |
|---|---|
| `reset_sweep` | page erase is ~22 ms, so 10 µs steps give ~2200 points per erase page. The cap `--max-points-per-phase` (default 50) still applies |
| `double_fault` | ROTATE happens every 16-20 updates, so ROTATE as the second fault gets real hits without a special setup |
| `bad_image` | `wrong_board` is the real F407 v2 image (section 2) |
| all | expected trial time about 3-5 s (erase ~90 ms, transfer ~7 KB at 115200 ≈ 1 s, one ST-Link read of 20 KB); measured 4.4-6 s per normal trial, longer with a resume or a golden flash (naive median ~16 s) |

**Wear:** slot pages are erased once per trial they are the target of. 5000 trials alternating
between A and B is about 2500 erases per slot page, and about 150-300 per meta page. The C0's
rated endurance is at least 10 000 cycles per page **(verify)**. The host keeps `wear.json` per
board, keyed by the hashed UID.

## 9. Output
Unchanged. `run.json` gains `"target": "c031"`. The summary title includes the target, and the
README puts the two boards in separate tables.

## 10. Code organisation

- Firmware: `firmware/boot_c0/`, its own `build_boot.py`, link script, `layout.h`, flash driver,
  timer and clock code. Copy or share the portable parts from `firmware/boot/` (crc32, sha256,
  b64, meta log logic, image validation, update protocol), but keep the hardware code separate.
  Shared files must compile for both cores. A later refactor can move them to `firmware/boot_common/`.
- Host: `python -m stress --target c031 ...` (default `f407`). `--target` selects:
  - `stress/layout_c031.py` (addresses, header size 256, header_crc offset 252, line limit 400,
    8-byte alignment);
  - the images directory `firmware/boot_c0/build/images/`;
  - the RAM top `0x20003000`.
  - The simulator gets the same parameters: page size 2 KB, double-word programming with the
    "only erased double-words" rule, ~22 ms erase and ~85 µs program times. The naive sim must
    still produce DEAD/WRONG.
  - Also selected by `--target c031` (host, v0.1.1): `[c031] images_dir` in stress.toml; the
    manifest entries must carry `"target": "c031"` (mkimage writes it per entry; F407 images have
    none), else `images_wrong_target`; the BL status line must say `target=c031`, else
    `wrong_firmware`; `calibration_c031.json` / `calibration_sim_c031.json`; the BEGIN reply timeout
    1 s; `run.json` `"target"`, `"link"`, `"reset_source"`. The bench config defaults to
    `[c031] bench_config` = `bench-c031.toml` and never falls back to bench.toml.
  - Sim (`stress/sim.py` with `layout_c031`): update-mode lines answered with `ERR LINE` (too long,
    a NUL byte, unknown) do not restart the 3 s inactivity timer (`firmware/boot_c0/bl_update.c`);
    a DATA length that is not a multiple of 8 answers `ERR SEQ <expected>`; a meta record is
    programmed as 24 bytes then the CRC double-word.
- Bench config: a separate `bench-c031.toml`, created by the human (bench.toml is protected) from
  `bench-c031.example.toml`, used with `--config bench-c031.toml` (the default for `--target c031`),
  containing:
  - `[serial] port` = the ST-LINK VCP (`/dev/serial/by-id/usb-STMicroelectronics_STLINK...`);
  - `[probe] openocd_target = "target/stm32c0x.cfg"`;
  - `[recovery] golden_image` = `firmware/boot_c0/build/images/golden_ab.hex`, `banner = '^BOOT '`.

## 11. Known blockers on this bench (checked 2026-11-07)

> **Update (later on 2026-11-07):** items 1 and 3 are resolved. OpenOCD built from git
> (`~/.local/openocd-git`, `target/stm32c0x.cfg`) is used through bench.py with `--config bench-c031.toml`,
> and the board is connected and running the stress tests. Item 2 (F407 constants in bench.py) still applies.

1. **OpenOCD 0.12.0 here has no `target/stm32c0x.cfg`** (`ls /usr/share/openocd/scripts/target`
   only shows `stm32g0x.cfg`). STM32C0 support was added after the 0.12.0 release. Options:
   build OpenOCD from git, or install STM32CubeProgrammer and use bench's `cubeprog` backend. The
   human decides; agents must not run either tool directly. Do not try `stm32g0x.cfg` on the C0:
   the flash IDs and page sizes differ.
2. **bench.py has F407-specific constants that are protected from agent edits:**
   - `READ_REGIONS`: its SRAM range is wider than the C0's 12 KB, and the system-memory range
     must cover the C0 UID at 0x1FFF7550.
   - `UID_ADDR = 0x1FFF7A10` is the F4 address.
   - `halt-dump` reads the CFSR/HFSR/MMFAR/BFAR registers, which the M0+ does not have.
   - The `stress` tool does not depend on these (it reads flash through `debug_commands` and
     takes the UID from the app's `UID` reply), but `bench.py mem` / `halt-dump` will be wrong or
     will refuse on the C0. That needs a human-approved bench.py change.
3. The C0 board is not connected yet; none of this has been tested on hardware.

## 12. Firmware decisions (`firmware/boot_c0/`, v0.1.1)

These fill gaps above; the host and the C031 simulator must mirror them.

- **Fault point indices.** `ERASE`: one fault point per slot erase (all 4 pages in one go), index =
  the slot's first page (8 = A, 12 = B); `T ERASE <page> <us>` covers the 4 pages. `ROTATE`: index =
  the meta page being erased (6 = META0, 7 = META1). The others are as in SPEC 6.2.
- **Fault timer.** TIM3 free-running over 0..0xFFFF with a preset counter and an overflow count
  (`noinit.ftimer`, after `expiry`, not covered by the noinit CRC). delay_us = 1 behaves like 2, as on
  the F407. Other noinit fields are unchanged.
- **Inactivity timer (update mode).** Restarts after every complete line that was not answered
  `ERR LINE` (SPEC-ESP32 1 "valid command"); a garbage stream or `ERR LINE` lines do not keep the BL
  in update mode. (The F407 restarts it on every received byte.)
- **Bad lines.** A line longer than the limit (400 in the BL, 96 in the app), with a NUL byte, or with
  a byte lost to an overrun/framing/noise/parity error is dropped: `ERR LINE` (BL), `ERR unknown
  command` (app). `\r` or `\n` ends a line; empty lines are skipped.
- **DATA.** `off` or decoded length not a multiple of 8 → `ERR SEQ <expected>` (section 4); the
  multiple-of-4 → `ERR SIZE` rule of SPEC 4 does not apply.
- **`reset=` names.** From RCC_CSR2: `LPWR,WWDG,IWDG,SFT,POR,PIN,OBL` (POR = PWRRSTF; the C0 has no
  separate BOR flag, OBLRSTF takes its place).
- **App `OPTR` command** → `OPTR 0x........` (FLASH_OPTR, read only), for the section 7 option byte
  check without `bench.py mem`.
- **`wrong_board_{A,B}.img`.** The real F407 v2 payload (padded to 8) with the F407 header fields
  (board `0xF407D15C`, link `0x08008200`/`0x0800C200`, 2.0.0, sc 1) **in the 256 B C0 header
  format**, so the C0 BL reaches the board check and answers `ERR BOARD`. (The raw 512 B F407 file
  would be `ERR BAD_HDR`: its header CRC is at 508, not 252.)
- **manifest.json** entries carry `"target": "c031"` in addition to the F407 keys.
