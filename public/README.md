# Published results

The raw data behind the results table in the [top-level README](../README.md). The tools write these
folders to `runs/` and `stress_runs/`, which are gitignored. This folder holds a copy of the runs that
the README's claims rely on. The tools' output was in Turkish when these runs were made, so the reports
use the Turkish outcome names (`YENİ`, `ESKİ`, `BEKLİYOR`, `ÖLÜ`, `YANLIŞ`; see the README's
Outcomes table).

Redactions: the host name, home directory, ST-Link serial number and board UIDs were replaced with
placeholders (`<host>`, `~/`, `<stlink-serial>`, `<board-uid-N>`). Nothing else was changed, so the
`audit_hash` values no longer match the files byte for byte.

## C031 bootloader stress test, hardware (2026-11-07) — `stress_runs/`

Each folder has `summary.md`, `summary.json`, `trials.csv`, `run.json` and `serial.log`.

| Folder | Result |
|---|---|
| `basic_20261107-120940` | 4/4 NEW |
| `transfer_cut_20261107-122009` | 10/10 OLD |
| `line_noise_20261107-122106` | 10/10 NEW at `--rate 0.2` |
| `bad_image_20261107-122229` | 2/2 |
| `rollback_20261107-122235` | 2/2 OLD |
| `reset_random_20261107-121105` | 20/20 (timer hook) |
| `reset_random_20261107-121251` | 20/20 (`--reset-source esp`) |
| `reset_sweep_20261107-122336` | 24/24 (ESP, 3 phases) |
| `reset_sweep_20261107-122531` | 57/57 (hook, 8 phases, `--meta-near-full`) |
| `double_fault_20261107-122248` | 10/10 |
| `naive_control_20261107-121450` | the control: 11/20 WRONG |
| `reset_sweep_20261107-152056` | 1493/1493 (2-hour sweep, hook, seed 2026) |
| `reset_sweep_20261107-172507` | 1479/1479 (2-hour sweep, ESP, seed 2027) |
| `calibrate_*_c031_*`, `calibration_c031.json` | phase timings and ESP windows the sweeps used |
| `wear.json` | flash erase counters |

Reset-injected A/B trials: 40 (`reset_random`) + 81 (short sweeps) + 10 (`double_fault`) + 1493 + 1479
= 3103, with 0 failures.

## F407 bench (2026-10-28) — `runs/`

Each folder has `report.md`, `result.json` and `serial.log`. Each passing plan comes with a
`_negatif` run, a deliberately broken copy that must FAIL (`--verify-can-fail`).

| Plan | Pass | Negative check (must fail) |
|---|---|---|
| `smoke` | `20261028-184415_smoke`, `20261028-184434_smoke` | `20261028-184417_smoke_negatif` |
| `crash_recovery` | `20261028-184419_crash_recovery`, `20261028-184434_crash_recovery` | `20261028-184422_crash_recovery_negatif` |
| `measure` | `20261028-184425_measure`, `20261028-184436_measure` | `20261028-184431_measure_negatif` |
| `camera_blink` | `20261028-190335_camera_blink` | `20261028-190443_camera_blink_negatif` |

The `camera_blink` folders also hold the phone videos (`cam_stepNN.mp4`), the frames with the LED
region marked (`cam_stepNN_roi.jpg`) and the brightness traces (`cam_stepNN.csv`).

## Documents — `stress/`, `foto/`, `docs/`

- `stress/SPEC.md`, `SPEC-C031C6.md`, `SPEC-ESP32.md`: the stress-test specs
- `stress/sunum.html`: the results slide deck (Turkish; open it in a browser)
- `foto/genel.jpg`: the C031 bench

Turkish PDFs in `docs/`:

- `tezgah-egitim.pdf`: the training booklet
- `tezgah-3000-deneme-raporu.pdf`: the 3103-trial report
