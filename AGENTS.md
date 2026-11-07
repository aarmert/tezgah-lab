# Agent rules (tezgah)

This folder has two STM32 test benches. Touch a board only through `bench.py` or `python -m stress`
(which uses bench.py). The bench's eye is an Android phone. The camera tool is in `camera/`.

## Folders
- `bench.py`, `bench.toml`, `plans/`, `runs/`: the test bench tool, its config, test plans, and results
- `firmware/`: the test firmware on the board (`stm32bench-fw`) and `build.py`
- `camera/`: the phone camera tool (Bun + TypeScript, scrcpy/adb/ffmpeg). The `cam_blink` step uses it.
- `.opencode/plugins/android-camera/`: the `android_take_photo` tool for OpenCode (uses the `camera/` code)
- `docs/raporlar/`: trial reports
- `stress/`, `stress_runs/`: the bootloader stress-test host tool and its results (`stress/README.md`)
- `firmware/boot_c0/`, `firmware/boot/`: stress-test bootloaders + apps for the C031 and the F407
- `esp32/bridge/`, `esp32/build.py`: the ESP32-S3 line bridge with a timed NRST
- `docs/stress/`: the stress-test specs (`SPEC.md` + `SPEC-C031C6.md` + `SPEC-ESP32.md`) and the results deck
- `docs/egitim/`: Turkish training booklet (PDF + its generator)

## Hardware
F407 bench (`bench.toml`, the `plans/` and `bench.py` commands below):
- Board: STM32F407G-DISC1 (STM32F407VG, Cortex-M4), LEDs on PD12-PD15
- Serial test channel: USART2, PA2=TX, PA3=RX, 115200 8N1 (through a USB-TTL adapter)
- Clock: HSI 16 MHz
- Camera: an Android 12+ phone with USB debugging enabled (adb serial `<ADB_SERIAL>`)

C031 bench (`bench-c031.toml`, used by `python -m stress --target c031`; running since 2026-11-07):
- Board: NUCLEO-C031C6 (STM32C031C6, Cortex-M0+, 32 KB flash in 2 KB pages, 12 KB RAM, 48 MHz) with
  its on-board ST-LINK/V2-1 (SWD + virtual COM port on USART2 PA2/PA3, 115200)
- ESP32-S3 bridge on its CH343 USB-UART port (921600): GPIO17 → PA10 (USART1 RX), PA9 → GPIO18,
  GPIO4 (open-drain) → NRST, GND. 3V3/5V are not connected between the boards.
- OpenOCD for the C0 is the git build in `~/.local/openocd-git` (the system 0.12.0 has no `stm32c0x.cfg`)
- `bench.py halt-dump` and `mem` have F407 constants and give wrong results on the C031

## Commands (always use --json)
- Build: `python firmware/build.py --json`
- Run a test plan: `python bench.py --json run smoke`
- When verifying a new feature: `python bench.py --json run <plan> --verify-can-fail`
- Try a single command: `python bench.py --json send "PING" --expect "^PONG$"`
- Reset and check the boot message: `python bench.py --json reset --expect "^BOOT"`
- If the board does not respond: `python bench.py --json recover`
- If the board is hung or crashed, see where it stopped: `python bench.py --json halt-dump`
  It prints pc, the fault registers with descriptions, the `stacked_frame` from the moment of the crash,
  and the code addresses on the stack, then resumes the core. Run this first and reset afterwards,
  because a reset erases the evidence.
- Read memory or a register: `python bench.py --json mem 0x40020C14 --count 1` (read only, allowed regions only)
- `bench.py gdbserver` is for humans only. Do not run it.
- Stress tool (C031 bench): `python -m stress --json doctor --target c031`, then
  `python -m stress --json run <test> --target c031 [--reset-source esp]`. Commands are in
  `stress/README.md`; `plan` and `report` do not touch the board, `--sim` runs without hardware.
- ESP32 bridge: build with `python3 esp32/build.py --json`. Uploading it is a human step.

Every result has `ok`, `error_type`, `summary`, and `likely_causes` fields. After a failed
`run`, read `serial.log` in the `report_dir`.

## Camera
- Take a photo: `cd camera && ~/.bun/bin/bun scripts/test-camera.ts <ADB_SERIAL> back`
  The last line is the JPEG path. Open the file and look at it. If the phone is not facing the board, try `front`.
- Record a video: `cd camera && ~/.bun/bin/bun scripts/record-video.ts <ADB_SERIAL> back 4 30`
  The arguments are seconds and fps, and the last line is the MP4 path. To measure an LED, use the
  `cam_blink` step instead of recording by hand.
- In OpenCode, the `android_take_photo` tool does the same job.
- If you changed the camera code, run `cd camera && ~/.bun/bin/bun test && ~/.bun/bin/bun run typecheck`.
  The tests use fake adb and scrcpy, so they need no phone.

## Prohibited
- Do NOT CHANGE the files in `bench.py`, `bench.toml`, `plans/`, or `runs/`. If a test is wrong, tell
  the human. Changing a test to make it pass is forbidden. The same applies to `camera/tests/`.
- Do not run `STM32_Programmer_CLI` or `openocd` directly.
- Do not upload to the ESP32 (`arduino-cli upload`, `esptool`, `mpremote`); a human does that.
- Never touch option bytes, read protection (RDP), or mass erase.
- In the firmware, do not use the PA13/PA14 (SWD) pins for anything else.

## Proposing a new test
If a new test is needed, write it to the `plans_proposed/` folder and tell the human.
The human moves it to `plans/` after approving it. Every new test must pass a negative check
with `--verify-can-fail`. A test that does not FAIL when the firmware is deliberately broken proves nothing.

## Work loop
1. Make the change under firmware/.
2. Build it. If there are errors, fix them.
3. Run the relevant plan.
4. If it FAILs, look at `likely_causes` and `serial.log` first, then fix. After 3 attempts on the
   same failure, stop and give the human a summary.
