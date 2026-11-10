# stress: bootloader stress test (host side)

Host half of the bootloader stress test for the STM32F407G-DISC1 bench and (`--target c031`) the
NUCLEO-C031C6 + ESP32-S3 bench. The contract with the firmware (`firmware/boot/`, `firmware/boot_c0/`,
`esp32/bridge/`) is [`public/stress/SPEC.md`](../public/stress/SPEC.md) with the addenda
[`SPEC-C031C6.md`](../public/stress/SPEC-C031C6.md) and [`SPEC-ESP32.md`](../public/stress/SPEC-ESP32.md).
Python 3.12+, stdlib + pyserial. matplotlib is optional (`report --plot`).

The tool uses `bench.py` without changing it: `Config`, `make_programmer` (OpenOCD backend only:
the flash reads use `dump_image`), `SerialTransport`, `BenchLock`, `BenchError`. A hardware run holds
the bench lock for the whole run, so no other `bench.py` command can touch the board meanwhile.

## Commands

Run from the repo root. Every command takes `--json` and returns `ok, error_type, summary, likely_causes`.

```
python -m stress --json doctor                        # images, manifest, calibration, bench.toml, probe, serial
python -m stress --json calibrate --design ab         # TRACE ON updates -> stress_runs/calibration.json
python -m stress --json calibrate --design naive
python -m stress --json plan reset_sweep --seed 1     # the deterministic fault list, nothing touched
python -m stress --json run basic --n 20 --seed 1
python -m stress --json run reset_sweep --seed 1      # --n defaults to every sweep point
python -m stress --json run naive_control --n 60      # always on the naive BL
python -m stress --json report stress_runs/reset_sweep_20261106-120000 --plot
```

Add `--sim` to any of them to run against the simulator instead of the board (see below).

`run <test>` options: `--n`, `--seed` (default 1), `--design ab|naive`, `--baseline any|strict`,
`--step-us` (sweep step, default 10), `--max-points-per-phase` (default 50), `--rate` (line_noise),
`--observe-s`, `--resume-limit-s`, `--dry-run` (plan and check config only).

| test | # | fault | pass |
|---|---|---|---|
| `basic` | 1 | none, alternates v1↔v2 | all NEW + smoke (PING/VER/TICK/UID) |
| `transfer_cut` | 2 | host stops at a random byte of the transfer | OLD, active slot hash unchanged |
| `line_noise` | 3 | corrupt/drop/delay(0-900 ms) per line with probability `--rate` | NEW |
| `reset_random` | 4a | `FAULT ARM 0 ERASE -1 <uniform over the update window>` | accepted only |
| `reset_sweep` | 4b | every phase × index × delay that occurs in the scenario (calibrated) | accepted only |
| `double_fault` | 5 | entry 0 as 4b, entry 1 at the reachable ones of META ROLLBACK/ATTEMPT/REPAIR, ROTATE, BOOT | accepted only |
| `bad_image` | 6 | bad_crc, wrong_board, truncated, oversize, old | the right `ERR`, OLD, hash unchanged |
| `rollback` | 7 | v2crash / v2hang / v2noconfirm | OLD after ≤ 3 trial attempts |
| `naive_control` | 8 | 4a + 4b on the naive BL | **must** show DEAD/WRONG |

`--meta-near-full` (reset_random, reset_sweep, double_fault; design ab) flashes S1..S4 of
`golden_ab_metafull` before every trial: META0 is one record short of full, so the update's records
rotate into META1 and ROTATE plus the META points after it are swept. Without it, `double_fault`
alternates the normal v1↔v2 update with an update to `v2crash` (rollback scenario), so META ROLLBACK
is reachable. `plan` shows per scenario which second faults are reachable (`second_faults`); REPAIR
never is. Trial notes carry `scenario=` and `fault2=fired|not_fired`.

`reset_*`, `double_fault`, `naive_control` and `calibrate` need the `hooks` builds (the BL status
line shows `hooks`). Run `calibrate` once per design, and again whenever the firmware changes
(`doctor` flags a calibration made with other images).

## One trial

1. **Baseline**: one OpenOCD call dumps SLOT A, SLOT B, META0, META1. `--baseline any` accepts a
   confirmed v1 or v2 in a valid active slot; `strict` wants v1 in A. Otherwise the golden image is
   flashed under reset. Then `VER` must match the flash, and `FAULT CLEAR` wipes leftover entries.
2. **Update**: `UPD` → `BL update` → `FAULT CLEAR`/`FAULT ARM` → `UPD BEGIN`, `UPD DATA` × n,
   `UPD END`, stop-and-wait with retries. The new image is the other version, in the inactive slot
   (naive: always A). With no armed fault and no cut, the host may start the whole update again (at
   most 3 times) when the BL gave up after lost lines (`BL timeout`); an unexpected reset is never
   retried. If `UPD` does not give `BL update` after 3 tries, the trial is a setup failure
   (UNCLEAR, note `setup_failed`); nothing is observed.
3. **Observe**: read lines until the board settles (confirmed app, no reset for `settle_s`), the BL
   waits in `update|resume|nofw`, or `observe_s` passes.
4. **Read + classify**: a second ST-Link read, then `classify.py`. When the window ends in
   `BL update|resume` with no jump/banner before it, the resume (step 5) starts first and the read
   comes after it: the BL waits only 3 s, an ST-Link read can take longer.
5. **PENDING**: the host finishes the update and must reach NEW within `resume_limit_s`. The
   updater never sends UPD commands blindly: if the lines since the last `BL update|resume` show the
   BL left update mode (`BL timeout`, jump, banner), it classifies that and, when the old app runs,
   enters update mode again with `UPD` (note `resume_reentered=<n>`).
   Then the boots are counted (SPEC 7): a BL status line no known reset explains makes the trial
   UNCLEAR (note `unexpected_reset=<n>`).
6. **Recovery** after DEAD, WRONG, UNCLEAR or a failed resume: reset first; if that does not
   give a usable baseline, flash `golden_<design>.bin` under reset, then verify with an ST-Link read.
   `stlink_needed=1`, `recovery_s` recorded. If recovery fails the run stops.

## How outcomes are classified

`classify.py` is a pure function with unit tests for every branch. The first match wins:

1. **WRONG**: the BL jumped to a slot whose expected-length hash is neither the old nor the new
   image, or an app banner's version disagrees with the hash of the slot it names.
2. **DEAD**: no `BOOT app` and no `BL update|resume|nofw` in the window.
3. **PENDING**: the window ends with the BL in `update|resume|nofw`. Accepted only if the resume
   reached NEW in time (`resume_ok=1`). A WRONG/DEAD during the resume wins over PENDING.
4. **OLD**: the running app is the old version and its slot holds the old image.
5. **NEW**: the running app is the new version and its slot holds the new image.
6. **UNCLEAR**: nothing above (for example the last banner was followed by a reset and nothing
   booted again), an unexpected reset, or a setup failure. Not accepted.

Accepted = OLD, NEW, PENDING with `resume_ok`. The test's own pass criterion (`expect_ok`) can be
stricter (table above). With the naive BL every jump into a half-written slot is WRONG by rule 1,
so the naive control shows mostly WRONG and few DEAD.

The outcome names were Turkish until 2026-11-10 (YANLIŞ = WRONG, ÖLÜ = DEAD, BEKLİYOR = PENDING,
ESKİ = OLD, YENİ = NEW, BELİRSİZ = UNCLEAR). Older `trials.csv` files keep them; `report` and the
`--plot` step map them to the English names when they read the file.

## Reading results

`stress_runs/<test>_<YYYYmmdd-HHMMSS>/` (gitignored):

- `summary.md`: PASS/FAIL with the reason, outcome counts, failure rate with a 95% upper bound,
  a phase × outcome table, update/recovery/resume/rollback times (median, p95), wear.
- `trials.csv`: one row per trial in the SPEC 9 column order. `note` holds the reasons in plain
  words plus `key=value` items (`rollback_s=`, `trial_jumps=`, noise counts, restarts).
- `serial.log`: every line in (`<`) and out (`>`) with a timestamp, plus host notes (`#`).
- `run.json`: arguments, seed, git commit + dirty flag, versions, firmware manifest + its sha256,
  calibration used, hashed board UID / ST-Link serial, and a `reproduce` command line.

Failure-rate bound for k failures in n trials: k = 0 gives the exact one-sided
`1 - 0.05^(1/n)` (shown with the rule of three `3/n`); k > 0 gives the one-sided Clopper-Pearson
bound plus the two-sided 95% CP interval. Example: 0 failures in 300 trials means "the failure
rate is below 1.0% with 95% confidence", not "it never fails".

`wear.json` counts slot erases per board across runs (one per BEGIN that may have erased, plus one
per golden flash). META erase counts come from the meta records themselves.

## Time estimates (F407 hardware, not yet measured)

On the C031 bench a normal trial measured 4.4-6 s (see above); the table below is the F407 estimate.


Per update trial ≈ 2 OpenOCD reads (~1 s each) + `UPD` reboot + erase (~0.3 s) + transfer
(~1 s for 7 KB at 115200 baud) + reboot, 1 s confirm and 1 s settle ≈ **6-9 s**. A PENDING adds
a resume (~4-6 s). A golden flash (recovery) adds ~5-10 s.

| run | trials | about |
|---|---|---|
| `basic --n 20` | 20 | 3 min |
| `transfer_cut --n 50` | 50 | 10 min (each waits for the 3 s BL timeout) |
| `reset_sweep` (defaults) | ~250 | 45-60 min |
| `double_fault --n 100` | 100 | 20 min |
| `rollback --n 150` | 150 | 60-80 min (noconfirm takes ~25 s each) |
| `naive_control --n 60` | 60 | 15 min (many golden flashes) |

## Simulator (`--sim`)

`sim.py` models both BL designs, the apps, and the F407 flash: erased = 0xFF, programming only
clears bits, a reset in the middle of an erase leaves a random mix of old and erased bits, and a
reset in the middle of programming leaves a partly programmed word. It also models the meta log with
rotation, the IWDG and the trial deadline, the fault hook (TIM2 one-shot, noinit entries that survive
resets) and UART timing, all in simulated microseconds. It plugs in as the serial transport, the
clock and the bench Programmer (it understands `dump_image` and `mdw`), so `--sim` runs the same host
code as hardware. Sim images are generated into `stress_runs/sim_images/`, and sim calibration goes
to `calibration_sim.json` (made automatically if missing).

What the sim shows (seed 1): `reset_sweep` on A/B gives 255/255 accepted (163 PENDING + 92 NEW),
and on naive 103/174 WRONG (every DATA and HDR point, and some partial erases). This is how the
naive control is meant to work. If the A/B sim ever shows a failure, either the sim or the spec
has a bug: report it.

## NUCLEO-C031C6 target and the ESP32-S3 bridge

`--target c031` (default `f407`) runs the same tests on the NUCLEO-C031C6 (`public/stress/SPEC-C031C6.md`)
with its own layout (`stress/layout_c031.py`: 2 KB pages, 256 B header, header_crc at 252, payload at
slot + 0x100, 8-byte alignment, one 400-char line limit, RAM top 0x20003000, board id 0xC031C600,
BEGIN reply 1 s), images (`[c031] images_dir`, default `firmware/boot_c0/build/images`; manifest
entries must say `"target": "c031"`), bench config (`bench-c031.toml`, from `bench-c031.example.toml`;
never bench.toml) and calibration (`calibration_c031.json`). The BL status line must say
`target=c031`, or the run stops (`wrong_firmware`). `run.json` records `target`, `link`, `reset_source`.

The C031 bench talks through the ESP32-S3 bridge (`esp32/bridge`, `public/stress/SPEC-ESP32.md`):

```
python3 esp32/build.py --json                                          # compile only
python -m stress --json doctor --target c031                            # images, bench-c031.toml, probe, VCP, ESP PING
python -m stress --json calibrate --target c031                         # over the bridge: hook traces + ESP windows
python -m stress --json run basic --target c031 --n 20
python -m stress --json run reset_sweep --target c031 --reset-source esp
python -m stress --json run reset_random --target c031 --link vcp       # no ESP32: everything over the VCP
```

- `--link esp` (default on c031): `stress/esp_bridge.py` wraps each line in `TX`, injected bytes in
  `TXH`, unwraps `RX`; `ESP ...` lines go to serial.log as `E`. The ST-LINK VCP is opened as the
  **witness** (logged as `V`): after each trial the host compares the status/banner lines it saw on
  both UARTs and notes `witness_mismatch: ...` when they differ. `--link vcp`: the VCP only.
- `--reset-source esp` (reset_random, reset_sweep, naive_control; needs `--link esp`): instead of
  `FAULT ARM`, the host sends `NRST ARM TX <delay> 1` right before the trigger line (ERASE → `UPD BEGIN`,
  DATA k → that DATA line, HDR → `UPD END`); the ESP pulses NRST `<delay>` µs after the line's last
  byte. CSV: `fault_type=nrst_esp`, `fault_fired` = pulses fired, note `reset_source=esp; late_us=<n>`.
  The fired pulses are explained resets for the boot count. The sweep windows per line come from
  `calibrate` over the bridge (`TIME ON` timestamps); double_fault always uses the hook.
- `[esp] port` / `baud` (921600) in stress.toml.

C031 sim (seed 1, every point): `reset_sweep` ab 288/288 accepted (167 PENDING + 121 NEW),
naive 98/174 WRONG; with `--reset-source esp` ab 150/150, naive 58/150 WRONG. `naive_control
--n 60`: 47 WRONG. `reset_random --n 100` and `double_fault --n 100`: all accepted.

C031 hardware (2026-11-07, `stress_runs/*_20261107-12*`, summary in `public/stress/sunum.html`): basic
4/4 NEW; transfer_cut 10/10 OLD; line_noise at `--rate 0.2` 10/10 NEW (48 retries); bad_image 2/2;
rollback 2/2 OLD; reset_random 20/20 with the hook and 20/20 with `--reset-source esp`; reset_sweep
24/24 (esp, 3 phases) and 57/57 (hook, all 8 phases with `--meta-near-full`); double_fault 10/10;
naive_control (esp) 11/20 WRONG. Then two 2-hour sweeps (stopped with Ctrl-C after 2 h):
`reset_sweep --seed 2026 --max-points-per-phase 800` (hook, 8 phases) 1493/1493 and the same with
`--seed 2027 --reset-source esp` (3 phases) 1479/1479. In total 0 failures in 3103 reset-injected A/B
trials: failure rate < 0.097% with 95% confidence. No ST-Link recovery and no unexpected reset on A/B;
9 `witness_mismatch` notes, all BOOT faults that cut the status line mid-character (the two UARTs keep
a different broken last byte; harmless). A normal trial takes 4.4-6 s; ESP NRST pulses were 2-15 µs
late (median 7 µs).

## Tests

```
python -m unittest discover -s stress/tests
```

They need no hardware: classifier branches, regexes, seeds, sweep enumeration, statistics (checked
against the binomial definition), CSV columns against SPEC.md, formats, sim flash semantics, and
end-to-end `--sim` runs of every test.
