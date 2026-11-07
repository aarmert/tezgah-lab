# stress: bootloader stress test (host side)

Host half of the bootloader stress test for the STM32F407G-DISC1 bench. The contract with the
firmware (`firmware/boot/`) is [`docs/stress/SPEC.md`](../docs/stress/SPEC.md); this tool follows it.
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
| `basic` | 1 | none, alternates v1↔v2 | all YENİ + smoke (PING/VER/TICK/UID) |
| `transfer_cut` | 2 | host stops at a random byte of the transfer | ESKİ, active slot hash unchanged |
| `line_noise` | 3 | corrupt/drop/delay(0-900 ms) per line with probability `--rate` | YENİ |
| `reset_random` | 4a | `FAULT ARM 0 ERASE -1 <uniform over the update window>` | accepted only |
| `reset_sweep` | 4b | every phase × index × delay that occurs in the scenario (calibrated) | accepted only |
| `double_fault` | 5 | entry 0 as 4b, entry 1 at the reachable ones of META ROLLBACK/ATTEMPT/REPAIR, ROTATE, BOOT | accepted only |
| `bad_image` | 6 | bad_crc, wrong_board, truncated, oversize, old | the right `ERR`, ESKİ, hash unchanged |
| `rollback` | 7 | v2crash / v2hang / v2noconfirm | ESKİ after ≤ 3 trial attempts |
| `naive_control` | 8 | 4a + 4b on the naive BL | **must** show ÖLÜ/YANLIŞ |

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
   (BELİRSİZ, note `setup_failed`); nothing is observed.
3. **Observe**: read lines until the board settles (confirmed app, no reset for `settle_s`), the BL
   waits in `update|resume|nofw`, or `observe_s` passes.
4. **Read + classify**: a second ST-Link read, then `classify.py`. When the window ends in
   `BL update|resume` with no jump/banner before it, the resume (step 5) starts first and the read
   comes after it: the BL waits only 3 s, an ST-Link read can take longer.
5. **BEKLİYOR**: the host finishes the update and must reach YENİ within `resume_limit_s`. The
   updater never sends UPD commands blindly: if the lines since the last `BL update|resume` show the
   BL left update mode (`BL timeout`, jump, banner), it classifies that and, when the old app runs,
   enters update mode again with `UPD` (note `resume_reentered=<n>`).
   Then the boots are counted (SPEC 7): a BL status line no known reset explains makes the trial
   BELİRSİZ (note `unexpected_reset=<n>`).
6. **Recovery** after ÖLÜ, YANLIŞ, BELİRSİZ or a failed resume: reset first; if that does not
   give a usable baseline, flash `golden_<design>.bin` under reset, then verify with an ST-Link read.
   `stlink_needed=1`, `recovery_s` recorded. If recovery fails the run stops.

## How outcomes are classified

`classify.py` is a pure function with unit tests for every branch. The first match wins:

1. **YANLIŞ**: the BL jumped to a slot whose expected-length hash is neither the old nor the new
   image, or an app banner's version disagrees with the hash of the slot it names.
2. **ÖLÜ**: no `BOOT app` and no `BL update|resume|nofw` in the window.
3. **BEKLİYOR**: the window ends with the BL in `update|resume|nofw`. Accepted only if the resume
   reached YENİ in time (`resume_ok=1`). A YANLIŞ/ÖLÜ during the resume wins over BEKLİYOR.
4. **ESKİ**: the running app is the old version and its slot holds the old image.
5. **YENİ**: the running app is the new version and its slot holds the new image.
6. **BELİRSİZ**: nothing above (for example the last banner was followed by a reset and nothing
   booted again), an unexpected reset, or a setup failure. Not accepted.

Accepted = ESKİ, YENİ, BEKLİYOR with `resume_ok`. The test's own pass criterion (`expect_ok`) can be
stricter (table above). With the naive BL every jump into a half-written slot is YANLIŞ by rule 1,
so the naive control shows mostly YANLIŞ and few ÖLÜ.

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

## Time estimates (hardware)

Per update trial ≈ 2 OpenOCD reads (~1 s each) + `UPD` reboot + erase (~0.3 s) + transfer
(~1 s for 7 KB at 115200 baud) + reboot, 1 s confirm and 1 s settle ≈ **6-9 s**. A BEKLİYOR adds
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

What the sim shows (seed 1): `reset_sweep` on A/B gives 255/255 accepted (163 BEKLİYOR + 92 YENİ),
and on naive 103/174 YANLIŞ (every DATA and HDR point, and some partial erases). This is how the
naive control is meant to work. If the A/B sim ever shows a failure, either the sim or the spec
has a bug: report it.

## Tests

```
python -m unittest discover -s stress/tests
```

They need no hardware: classifier branches, regexes, seeds, sweep enumeration, statistics (checked
against the binomial definition), CSV columns against SPEC.md, formats, sim flash semantics, and
end-to-end `--sim` runs of every test.
