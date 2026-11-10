# Bootloader stress test: specification (v0.1.2)

This is the contract between the firmware (`firmware/boot/`) and the host stress tool (`stress/`).
Both sides must follow it exactly. If you need to change it, change this file in the same commit
and say so in your report.

Target: **STM32F407G-DISC1** (STM32F407VG, 1 MB flash, 192 KB RAM (128 KB SRAM + 64 KB CCM), HSI 16 MHz).
Serial: USART2 PA2/PA3 through the USB-TTL, 115200 8N1. This is the same port `bench.py` uses.
Phase 1 has no ESP32. The PC is the updater and injects the faults. Precise reset timing comes from
the **in-target fault hook** (section 6). The FAULT protocol is written so an ESP32 can implement
the same commands later.

## 1. Flash layout (F407 sectors)

| Sector | Address | Size | Use |
|---|---|---|---|
| S0 | 0x08000000 | 16 KB | Bootloader (BL) |
| S1 | 0x08004000 | 16 KB | META0 |
| S2 | 0x08008000 | 16 KB | SLOT A |
| S3 | 0x0800C000 | 16 KB | SLOT B |
| S4 | 0x08010000 | 64 KB | META1 |
| S5.. | 0x08020000 | — | unused |

16 KB slots keep the erase short (~0.25-0.5 s instead of 1-2 s for a 128 KB sector), so trials run
faster. All addresses live in a single `firmware/boot/layout.h`, mirrored in `stress/layout.py`.
The naive bootloader uses only SLOT A, in place.

RAM: the first 256 bytes (0x20000000-0x200000FF) are a shared `.noinit` area (section 6.3). The BL
and every app link script reserve it and never zero it.

## 2. Image format

A slot is `[header 512 B][payload]`. The payload is a normal Cortex-M image (vector table first)
linked to run at `slot_base + 0x200`. 512 B keeps VTOR aligned. The BL refuses an image whose
`link_addr` is not the target slot's payload address, so every app is built twice (for A and B).

Header (little-endian, 64 bytes used, the rest of the 512 is 0xFF):

| Off | Type | Field |
|---|---|---|
| 0 | u32 | magic `0x4D495A54` ("TZIM") |
| 4 | u16 | header_version = 1 |
| 6 | u16 | flags (0) |
| 8 | u32 | board_id = `0xF407D15C` |
| 12 | u32 | fw_version = (major<<16)|(minor<<8)|patch |
| 16 | u32 | security_counter (anti-rollback, see 4.4) |
| 20 | u32 | link_addr (payload address) |
| 24 | u32 | payload_size (bytes, multiple of 4) |
| 28 | u32 | payload_crc32 (IEEE 802.3, the same as zlib.crc32) |
| 32 | u8[32] | payload_sha256 |
| 64.. | | (0xFF up to 508) |
| 508 | u32 | header_crc32 over bytes 0..507 |

A slot is **valid** when: magic is OK, header_crc is OK, board_id matches, link_addr equals this
slot's payload address, payload_size is at most 16384-512, and the payload CRC32 matches. The BL
checks the CRC32 on every boot. It checks the SHA-256 once, at `UPD END`.

Images (made by `firmware/boot/mkimage.py`, written to `firmware/boot/build/images/`):

| Name | Content |
|---|---|
| `v1_A.img`, `v1_B.img` | app 1.0.0, security_counter 1 |
| `v2_A.img`, `v2_B.img` | app 2.0.0, security_counter 1 |
| `v2crash_{A,B}.img` | 2.0.0-crash: HardFault right after the banner, before it confirms |
| `v2hang_{A,B}.img` | 2.0.0-hang: endless loop after the banner, does not feed the IWDG |
| `v2noconfirm_{A,B}.img` | 2.0.0-noconfirm: runs normally, never confirms |
| `bad_crc_{A,B}.img` | v2 with one payload byte flipped after the header was made |
| `wrong_board_{A,B}.img` | v2 with board_id `0xC0C0C0C0` (header CRC still valid) |
| `truncated_{A,B}.img` | v2 header, but only the first half of the payload |
| `oversize_{A,B}.img` | header payload_size = 20000 (bigger than the slot), padded payload |
| `old_{A,B}.img` | app 0.9.0, security_counter 0 |
| `golden_ab.bin` | 0x08000000..0x0801FFFF: BL(ab), META0 with an initial record, v1_A in A, B erased (0xFF), META1 erased |
| `golden_naive.bin` | the same with the naive BL and v1 in A (naive images use the same header format) |
| `golden_ab_metafull.bin` | `golden_ab` with META0 holding 511 of its 512 records (seq 1 CONFIRMED, 2..511 CONFIRM, all active=A, min_sc 1): the next update's TRIAL takes the last slot and the following append (the trial boot's ATTEMPT) rotates into META1 (test 4b/5 `--meta-near-full`) |
| `golden_*.hex` | the same bytes as Intel HEX with addresses (OpenOCD `program` needs an address for a raw .bin) |
| `golden_{ab,naive}_nohooks.{bin,hex}` | a release board: the release BL **and the release v1 app** (both built without `-DFAULT_HOOKS`). `golden_ab`/`golden_naive` use the `hooks` BL and apps |
| `release/` | the whole image set above (same file names, own `manifest.json`) built from the release apps, with `golden_ab`/`golden_naive` = release BL + release v1. Tests that need no hooks (1, 2, 3, 6, 7) run on a release board with `[stress] images_dir = "firmware/boot/build/images/release"` |
| `manifest.json` | name → {sha256 of the whole file, sha256 of the payload, version, slot, size} |

The host computes **slot hashes** over `[slot_base, slot_base + 512 + payload_size_of_the_expected_image)`,
and also over the whole 16 KB slot. The CSV records the whole-slot hash, plus a match label
computed from the expected-length hash (`v1`, `v2`, `empty`, `other`; when a trial's image is
another one, e.g. `v2crash` or `bad_crc`, its base name is the label).

## 3. Serial lines printed by the firmware

All lines end with `\r\n`. These regexes are the parsing contract (`stress/proto.py`):

BL, on every boot, exactly one status line:
```
BL v0.1 design=(ab|naive)(?: hooks)?(?: target=(f407|c031))? slotA=(ok:\d+\.\d+\.\d+|bad|empty) slotB=(ok:..|bad|empty|-) active=(A|B|-) state=(confirmed|trial|none) attempts=\d+ seq=\d+ erases=\d+,\d+ reset=\S+
```
(naive prints `slotB=- state=none attempts=0 seq=0 erases=0,0`). The F407 firmware prints
`target=f407`; the host also accepts the line without `target=` (older builds) and reads it as f407
(SPEC-C031C6 §3). `slotX=empty` means the whole 16 KB slot is erased; a slot that is neither valid nor
fully erased is `bad`. The naive BL prints `slotA=ok:<version>` whenever the header magic is right
(it never checks a CRC). `reset=` lists the RCC_CSR flags in the order
`LPWR,WWDG,IWDG,SFT,POR,PIN,BOR` (a software reset reads `SFT,PIN`: SYSRESETREQ also pulses NRST).

Hooks builds then print the fault report of section 6.1, if there is anything to report:
```
BL fault_fired <n> <PHASE> <idx> <delay>          (n fault timers expired since the last report)
BL fault_cancelled <n> <PHASE> <idx> <delay>      (n started fault timers never expired)
```

Then exactly one decision line:
```
BL jump (A|B)( trial)?
BL rollback (A|B)->(A|B)          (followed by "BL jump X")
BL repair active=(A|B)            (active slot invalid, the other one is valid; followed by "BL jump X")
BL update                         (update mode, waiting for UPD commands)
BL resume                         (update interrupted by a reset; waits 3 s for UPD BEGIN, then boots normally)
BL nofw                           (no valid slot; stays in update mode)
BL timeout                        (update mode had 3 s of inactivity; it then boots normally)
```

App:
```
BOOT app (\d+\.\d+\.\d+)(?:-\w+)? slot=(A|B) trial=(0|1) reset=\S+
APP confirmed
HARDFAULT pc=0x........ lr=0x........ cfsr=0x........ hfsr=0x........
```
The `BOOT ` prefix also matches bench.toml's recovery banner `^BOOT `.

App commands: `PING`→`PONG`, `VER`→`VER x.y.z`, `TICK`→`TICK <ms>`, `UID`→`UID <24 hex>`,
`RESET`→`OK RESET` then reset, `CRASH`→`OK CRASH` then HardFault,
`UPD`→`OK UPD`: sets the noinit "enter update" flag and resets into the BL, which prints `BL update`.
`STATE`→`STATE active=A trial=0 confirmed=1 seq=..`. `FAULT ...` (section 6). Unknown → `ERR unknown command`.

## 4. Update protocol (handled in the BL)

Stop-and-wait. The host sends one line and waits for the reply before sending the next. The BL does
not read the UART while it erases or programs, so the host must never pipeline. Timeouts for one
reply: 3 s after BEGIN (the erase), 1 s otherwise including END. END can take up to ~1.1 s when its
TRIAL append rotates into the 64 KB META1; the host then retries END and accepts the late
`OK COMMIT`. END must stay well below the BL's 3 s inactivity timeout. The BL leaves update mode after 3 s with no
valid command (`BL timeout`).

| Host | BL reply | Notes |
|---|---|---|
| `UPD BEGIN <b64 of the 512 B header>` | `OK ERASED <us>` or `ERR <code>` | validates the header, erases the target slot |
| `UPD DATA <off> <b64 ≤192 B> <crc32 hex8>` | `OK <off>` / `ERR CRC <off>` / `ERR SEQ <expected>` | off is relative to the payload start, multiple of 4 |
| `UPD END` | `OK COMMIT` then reset, or `ERR <code>` | full check, writes the header, then the TRIAL meta record |
| `UPD ABORT` | `OK ABORT` | no reset and no new status line: the BL leaves update mode and its boot decision (4.2) goes on, e.g. `BL jump A` |

- Target slot: A/B uses the inactive slot. Naive always uses A.
- DATA is idempotent. If `off + len` is at most the bytes already written and the flash content
  equals the data, the reply is `OK <off>` again. That lets the host retry after a lost reply.
  `off` beyond the next expected offset → `ERR SEQ <expected>`.
- The chunk CRC is crc32 of the decoded bytes. Lines longer than 300 chars → `ERR LINE`, except
  `UPD BEGIN` lines, which may be up to 700 chars (the base64 of the 512 B header alone is 684 chars).
- Any other line the BL does not recognise in update mode (not `UPD BEGIN/DATA/END/ABORT`, `PING`,
  or, in hooks builds, `FAULT ...`) → `ERR LINE`. Lines are compared exactly (no trimming).
- DATA checks, in this order (the first failing one answers): malformed (`<off>` not a decimal u32,
  empty base64 field, `<crc32>` not 1-8 hex digits, or anything after it) → `ERR LINE`; no BEGIN
  session → `ERR STATE`; base64 that does not decode (or decodes to 0 or more than 192 bytes) or a CRC
  mismatch → `ERR CRC <off>`; `off` not a multiple of 4, or beyond the next expected offset →
  `ERR SEQ <expected>`; decoded length not a multiple of 4, or `off + len` past the image size →
  `ERR SIZE`; then the idempotent retry check, then programming.
- Any END error (`SHORT`, `IMG_CRC`, `IMG_SHA`, `FLASH`) ends the session: a later DATA/END gets
  `ERR STATE` until the next BEGIN.
- The host takes as the reply only what the command can produce (BEGIN: `OK ERASED` or `ERR`
  BAD_HDR/BOARD/SIZE/LINK/VERSION/FLASH/LINE; END: `OK COMMIT` or `ERR` SHORT/IMG_CRC/IMG_SHA/FLASH/
  STATE/LINE). Anything else is stale, e.g. the `ERR STATE` for a DATA line that reached the BL just
  after a reset, and is skipped.
- Error codes: `BAD_HDR`, `BOARD`, `SIZE`, `LINK`, `VERSION`, `CRC <off>`, `SEQ <n>`, `SHORT`,
  `IMG_CRC`, `IMG_SHA`, `LINE`, `STATE`, `FLASH`.
- **A/B commit order** (this is the safety argument): payload → END verifies CRC + SHA by reading
  the flash back → header written last → TRIAL meta record appended (its CRC word is written last) →
  `OK COMMIT` → reset. A reset before the header is complete leaves the slot invalid, and a reset
  before the meta CRC word is written leaves the old record as the latest one. Both cases boot the
  old image.
- **Naive** (negative control, deliberately unsafe): BEGIN checks only the header magic, the header
  CRC (`BAD_HDR`) and payload_size (`SIZE`) — no board, link, version or security-counter check —
  then erases SLOT A in place, DATA writes into it, END writes the header with **no check**,
  `OK COMMIT`, reset. The naive BL has **no
  `update_in_progress` flag and never prints `BL resume`**: after a reset it boots slot A on the
  word-0 check below, or prints `BL nofw`. (A resume would let the host repair almost every
  interrupted naive update, and the negative control would measure little.) The naive BL boots slot A when
  word 0 of the payload looks like a RAM stack pointer. It skips the CRC check, so a half-written
  image can run (WRONG) or crash (DEAD).

### 4.1 Meta records (A/B)

META0 and META1 are append-only logs of 32-byte records. The latest record with a valid CRC and the
highest `seq` (searched in both sectors) holds the state.

| Off | Type | Field |
|---|---|---|
| 0 | u32 | magic `0x4154454D` ("META") |
| 4 | u32 | seq |
| 8 | u8 | kind: 1 CONFIRMED, 2 TRIAL, 3 ATTEMPT, 4 CONFIRM, 5 ROLLBACK, 6 REPAIR |
| 9 | u8 | active (0=A, 1=B) |
| 10 | u8 | trial (0=A, 1=B, 0xFF none) |
| 11 | u8 | attempts |
| 12 | u32 | min_security_counter |
| 16 | u32 | erase_count of the sector this record is in |
| 20 | u32 | erase_count of the other meta sector |
| 24 | u32 | reserved (0xFFFFFFFF) |
| 28 | u32 | crc32 over 0..27, **programmed last** |

Rotation: when the current sector is full, erase the other sector, then write a snapshot record
there with its erase_count + 1. The old sector stays until the next rotation. A reset during
rotation must never lose the state: the old sector is still intact.

### 4.2 Boot decision (A/B)
1. Load the meta. If no record is valid: boot whichever slot is valid (A first) and append a
   CONFIRMED record. If neither is valid → `BL nofw`.
2. The noinit flag `enter_update` → `BL update`. The noinit flag `update_in_progress` (set at BEGIN,
   cleared at COMMIT or ABORT) → `BL resume`.
3. trial ≠ none: if attempts ≥ 3 or the trial slot is invalid → append ROLLBACK (trial=none), print
   `BL rollback T->active`, boot active. Otherwise append ATTEMPT (attempts+1), start the IWDG at
   about 2 s (LSI, prescaler 32, reload 2000), and `BL jump T trial`.
4. Otherwise boot active if it is valid. If it is not and the other slot is valid → REPAIR record →
   `BL repair active=X`. If neither is valid → `BL nofw`.

### 4.3 App runtime (shared `app_rt.c`)
- If the IWDG is running (the BL started it), feed it from the SysTick main loop.
- A trial app (meta trial == own slot) appends CONFIRM (active=own, trial=none, attempts=0) after
  1 s of healthy running and prints `APP confirmed`.
- Trial deadline: a trial app that has not confirmed after 5 s stops feeding the IWDG, so the
  watchdog resets it. That makes noconfirm roll back after 3 attempts.
- v2crash: `HARDFAULT ...` line, then `NVIC_SystemReset`. v2hang: `while(1)` with no feeding.

### 4.4 Anti-rollback
BEGIN rejects with `ERR VERSION` when header.security_counter < meta.min_security_counter. CONFIRM
raises min_security_counter to the confirmed image's counter. v1 and v2 both use 1, so they can
alternate. `old` uses 0 and is always rejected.

## 5. Flash code runs from RAM
On the F407, a CPU fetch from flash stalls while that flash is being erased or programmed. Flash
erase/program routines, the TIM2 fault ISR, and a RAM copy of the vector table (VTOR switched to RAM
during flash operations) must all run from RAM (`.ramfunc`). Otherwise the fault hook could not fire
in the middle of an erase.

## 6. Fault hook (`-DFAULT_HOOKS`, test builds only)

The `hooks` builds of the BL and the apps accept FAULT commands. Release builds (`bl_ab`, `bl_naive`,
`app_<variant>_<slot>_nohooks`) do not: no fault points, no FAULT/TRACE commands (`ERR LINE` in the
BL, `ERR unknown command` in the app), TIM2 is the default handler. The BL status line shows the
word `hooks` only when they are compiled in. The update images in `images/` come from the hooks
apps (the CONFIRM fault point lives in the app); `images/release/` holds the same set from the
release apps.

### 6.1 Commands (accepted in update mode and by the app)
```
FAULT ARM <n:0|1> <PHASE> <index|-1> <delay_us> [reset]   -> OK FAULT ARM n   (reset = the fault action; the default and the only one)
FAULT CLEAR                                               -> OK FAULT CLEAR
FAULT LIST                                                -> FAULT n PHASE idx delay | FAULT n -
FAULT TRACE <ON|OFF>                                      -> OK FAULT TRACE
```
When the code reaches `fault_point(PHASE, index)` and an armed entry matches (index -1 = any), the
entry is **disarmed in noinit first**, the start is recorded (`noinit.pending`, with a new sequence
number), then TIM2 (1 MHz, one-shot) starts with `delay_us`. On expiry the ISR (in RAM, no flash
calls) writes the expiry record (`noinit.expiry`, outside the CRC-covered part, with its own magic
and check word) and does SYSRESETREQ, which also pulses NRST. delay_us = 0 records the expiry and
resets immediately. delay_us = 1 behaves like 2 (TIM2 needs ARR ≥ 1). Only one timer runs at a time;
while it is pending, other entries stay armed. Entry 1 stays armed across the reset, which gives a
double fault (test 5).

A fault counts as **fired** only when its timer expired. A started timer that never expired — the
board reset for another reason first (`OK COMMIT`, `OK UPD`, IWDG, ...) or `FAULT CLEAR` stopped
it — counts as **cancelled**. At BL entry, before the `BOOT` fault point, the BL decides which of the
two the last start was and takes a snapshot of both counts (nothing is cleared yet, so a `BOOT`
fault that resets on the spot loses nothing: the next boot reports the sum). After the status line
it prints `BL fault_fired n PHASE idx delay` and/or `BL fault_cancelled n PHASE idx delay` (n since
the last report, details of the last one) and then subtracts what it reported.

### 6.2 Phases (fault points)
| PHASE | Where | index |
|---|---|---|
| `ERASE` | start of a slot sector erase (BEGIN) | sector number |
| `DATA` | start of programming chunk k | k (0-based chunk count) |
| `HDR` | start of the header write (END) | 0 |
| `META` | start of a meta record append | kind |
| `ROTATE` | start of a meta sector erase during rotation | sector number |
| `BOOT` | BL entry, before the decision | 0 |
| `JUMP` | right before the jump to the app | slot |
| `CONFIRM` | in the app, right before its CONFIRM append | 0 |

With TRACE ON, the code prints `T <PHASE> <idx> <us>` after each one, where us is that operation's
duration (from the DWT cycle counter). T lines come before the reply of the command that caused them
(e.g. `T ERASE 3 <us>` then `OK ERASED <us>`), so the host skips lines starting with `T `.
`T BOOT 0 <us>` is the time from BL entry to the decision; `T JUMP` is ~0. `stress calibrate` uses these to find the phase durations
for the sweep.

### 6.3 noinit layout (0x20000000, 256 B; magic `0x4E4F494E` plus crc32)
`magic, enter_update, update_in_progress, fault[2]{armed, phase, index, delay_us},
pending{active, seq, phase, index, delay_us}, cancelled{count, phase, index, delay_us}, trace,
reset_csr, crc32`, then, **not covered by crc32**, `expiry{magic 0x44505845, count, seq, phase,
index, delay_us, check}` (written only by the TIM2 ISR / the delay-0 path; cleared on a power-on or
brown-out reset). `reset_csr`: RCC_CSR as seen by the BL, which clears the flags; the app prints its
`reset=` from it. The exact struct is in `firmware/boot/noinit.h`,
mirrored in the host's documentation only. The host never reads it.

## 7. Outcome classification (host, deterministic, `stress/classify.py`)

Inputs: the serial lines seen during the observe window, an ST-Link read of both slots and both
meta sectors, the manifest hashes, and the expected old and new image.

Precedence (the first match wins):
1. **WRONG**: the BL said `jump X` (or naive jumped to A) and slot X's expected-length hash matches
   neither the old nor the new image. Also WRONG: the app banner's version disagrees with the hash
   of the slot it reports.
2. **DEAD**: no `BOOT app` line within the observe window (default 15 s) and no `BL update|resume|nofw`.
   The tool then recovers the board over ST-Link (`stlink_needed=1`).
3. **PENDING**: the BL is in `resume`/`update`/`nofw`. The host then finishes the update (resume).
   The trial is accepted if the final result is NEW within `resume_limit_s` (default 30 s). When
   the new image is a failing variant (`v2crash`, scenario `rollback` of test 5), the goal is OLD
   after the resumed update committed and the BL rolled back.
   `resume_ok` and `resume_s` are recorded.
4. **OLD**: the running app is the old version, and its slot hash equals the old image.
5. **NEW**: the running app is the new version, and its slot hash equals the new image.
6. **UNCLEAR**: none of the above (e.g. the last app banner was followed by a reset and nothing
   booted again). Not accepted; the tool recovers the board.

"Running app" is the last `BOOT app` line with no BL status line or `HARDFAULT` after it. If the
resume (rule 3) itself shows WRONG or DEAD, that outcome wins.

Accepted: OLD, NEW, and PENDING with resume_ok. Test-specific expectations (section 8) can be
stricter (test 2 requires that the active slot's hash is unchanged).

**Resume timing.** When the observe window ends in `BL update|resume` and it holds no `BL jump` or
`BOOT app` line (rule 1 has nothing to check), the host starts the resume at once and does the
ST-Link read afterwards: the BL waits only 3 s, and an ST-Link read can take longer. If the BL has
already left update mode when the resume starts (`BL timeout`, then it booted), the host does not
send UPD commands to the app: it classifies that window, and if the old app runs (OLD) it sends
`UPD` to enter update mode again; the CSV note says `resume_reentered=<n>`.

**Unexpected reset** (checked over the whole trial, from the first `UPD` to the end of the resume):
every reset makes the BL print one status line, so the host counts status lines (boots) and the
resets it can explain: the app's `OK UPD`/`OK RESET` and the BL's `OK COMMIT` replies, app
`HARDFAULT` lines, the n of every `BL fault_fired n ...` line, and status lines whose `reset=` holds
`IWDG`. More boots than explained resets → **UNCLEAR**, not accepted, note `unexpected_reset=<n>`,
unless the outcome is already WRONG or DEAD (those stay). Fewer boots is fine (a reset can come
before the status line is printed).

**Setup failure.** If the host cannot get `BL update` after 3 `UPD` attempts (the BL booted
instead), nothing was updated and there is no window to observe: the trial is recorded as
UNCLEAR, not accepted, note `setup_failed: ...`, and the board is recovered.

**The classifier is code, never an agent's judgement.** It has unit tests for every branch.

## 8. Tests (`python -m stress --json run <name> --n N --seed S [--design ab|naive] [--sim]`)

| name | # | Fault | Pass criterion |
|---|---|---|---|
| `basic` | 1 | none, alternates v1↔v2 | 100% NEW, smoke (PING/VER/TICK/UID) passes, update_s and throughput recorded |
| `transfer_cut` | 2 | host stops sending at a random byte of the transfer | OLD, and the active slot hash is unchanged |
| `line_noise` | 3 | host corrupts a byte, drops a line, delays (0-900 ms), random per line at a rate p | NEW, retries counted |
| `reset_random` | 4a | FAULT ARM at phase ERASE idx -1 with a uniform delay over the measured whole-update duration (it chains: the first ERASE point plus a delay that can exceed it; the BL keeps the timer running across phases) | OLD/NEW/PENDING-ok only |
| `reset_sweep` | 4b | every phase × index **that occurs in the trial's scenario**, delay 0..duration step `--step-us` (default 10), capped at `--max-points-per-phase` (default 50, evenly spaced) | same |
| `double_fault` | 5 | entry 0 as in 4b, entry 1 drawn from META ROLLBACK / ATTEMPT / REPAIR / ROTATE / BOOT **restricted to the points that occur in the trial's scenario** (see below) | same |
| `bad_image` | 6 | each bad image × 20 | `ERR <expected code>`, v1 keeps running, hash unchanged |
| `rollback` | 7 | v2crash / v2hang / v2noconfirm × 50 | back on v1 (OLD) after ≤3 attempts; rollback_s recorded (observe window 45 s: noconfirm needs 3 × (5 s deadline + 2 s IWDG)) |
| `naive_control` | 8 | 4a + 4b on the naive BL | **must** show DEAD and/or WRONG. If not, the test measures nothing |

Expected codes for 6: bad_crc→`IMG_CRC`, wrong_board→`BOARD`, truncated→`SHORT`, oversize→`SIZE`,
old→`VERSION`.

**Scenarios** (tests 4a, 4b, 5). A fault point that never occurs in a trial never fires, and such a
trial passes trivially. So the calibration records, per scenario, which points occur (`seen`), and
only those are swept or drawn:
- `normal`: v1↔v2 from the usual baseline. Reaches ERASE, DATA, HDR, META TRIAL/ATTEMPT/CONFIRM,
  JUMP, CONFIRM, BOOT. ROTATE practically never (META0 holds ~170 updates), ROLLBACK and REPAIR never.
- `metafull` (`--meta-near-full`, design ab): before every trial the host flashes S1..S4 of
  `golden_ab_metafull` (the BL sector is not touched) and checks that META0 has at most 2 free
  records. The update's TRIAL fills META0 and the next append rotates, so ROTATE and the META points
  after it (ATTEMPT, CONFIRM in META1) are swept.
- `rollback` (test 5 only, every odd trial unless `--meta-near-full`): the update goes to `v2crash`,
  so three crashed attempts are followed by a ROLLBACK record; META ROLLBACK is reachable.
- REPAIR needs an invalid active slot, which no update reaches: it is listed as unreachable.

`calibrate` (design ab) traces `--runs` normal updates plus one `metafull` and one `rollback`
update. `plan`/`run.json` list per scenario the reachable and unreachable second faults
(`second_faults`); the CSV note column carries `scenario=<s>` and, for test 5, `fault2=fired|
not_fired|ambiguous`.

Baseline: before each trial the host reads the slot and meta state over ST-Link (one OpenOCD call).
`--baseline any` (default) accepts "a confirmed v1 or v2 runs from a valid slot" and updates to the
other version. `--baseline strict` requires v1 in A, and flashes `golden_ab.bin` when it differs.

Seeds: `trial_seed = int.from_bytes(sha256(f"{seed}:{test}:{trial}").digest()[:8], "little")`,
used with `random.Random(trial_seed)`. Sweep points are enumerated deterministically, and `--seed`
only shuffles their order. A run can be repeated exactly from (seed, test, n, firmware commit).

## 9. Output

`stress_runs/<test>_<YYYYmmdd-HHMMSS>/` (gitignored):
- `trials.csv`, one row per trial, columns in this order:
  `trial,seed,trial_seed,test,design,fault_type,phase,phase_index,delay_us,byte_offset,fault2_phase,fault2_index,fault2_delay_us,from_version,to_version,image,outcome,accepted,expect_ok,running_version,running_slot,slotA_sha256,slotB_sha256,slotA_match,slotB_match,meta_seq,meta_state,attempts,stlink_needed,resume_ok,resume_s,recovery_s,update_s,bytes,throughput_Bps,retries,fault_fired,err_code,meta_erases0,meta_erases1,slot_erases_A,slot_erases_B,trial_s,bl_line,app_line,note`
  (slot_erases_* is counted by the host: one per BEGIN on that slot, plus the history in `stress_runs/wear.json`).
- `summary.json` / `summary.md`: counts per outcome, per phase × outcome table, failure rate with
  a 95% upper bound (zero failures: exact `1-0.05^(1/N)`, shown next to the rule-of-three `3/N`;
  otherwise Clopper-Pearson), median and p95 update_s / recovery_s, wear table.
- `serial.log` (full transcript, with host notes), `run.json` (args, seed, git commit + dirty flag,
  tool versions, firmware manifest hashes, bench name). Board UID and ST-Link serial are stored only as
  `sha256(salt + value)[:12]` (salt from `stress.toml`).
- `python -m stress report <run_dir> [--plot]` rebuilds summary.md. `--plot` writes PNGs with
  matplotlib if it is installed, and otherwise says so.

## 10. Host tool rules
- `stress/` imports `bench.py` (Config, make_programmer, SerialTransport/Session, BenchLock, BenchError)
  and **does not modify it**. ST-Link access goes only through the bench Programmer
  (`debug_commands` with `dump_image`, `flash(under_reset=True)`, `reset`).
- Config: `stress.toml` (gitignored, `stress.example.toml` committed), plus bench.toml for the probe/serial.
- `--sim` uses `stress/sim.py`. It is a Python model of both BL designs, the apps, flash (erased=0xFF,
  program clears bits, erase timing, partial writes on a reset mid-operation) and the fault hook,
  with simulated time. It must give the C firmware's replies and behaviour line for line (check
  order, parsing, boot decision order, reset flags); `stress/tests/test_sim_contract.py` and
  `firmware/boot/tests` pin both sides. Sim timings can be overridden in stress.toml `[sim]`
  (e.g. `stlink_read_s = 3.5`). This lets everything run with no hardware and powers the unit tests. The
  naive sim must really produce DEAD/WRONG.
- Every command supports `--json` with `ok, error_type, summary, likely_causes` like bench.py.
- `--target f407|c031` (default f407) and, for the C031 bench, `--link esp|vcp` and
  `--reset-source hook|esp`: see `SPEC-C031C6.md` and `SPEC-ESP32.md`. The F407 behaviour is unchanged.
