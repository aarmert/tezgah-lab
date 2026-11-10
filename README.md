# tezgah

*Tezgah* is Turkish for workbench. This repo is a hardware-in-the-loop test bench for STM32 boards
that a person or a coding agent can drive from the command line. It flashes and resets the board,
talks to it over a serial port, watches its LEDs through an Android phone camera, and runs test plans.
Every command takes `--json` and returns a machine-readable verdict with the likely causes of a
failure, so an agent can act on the result without reading a log. Every test also has to show that it
can fail: each plan is run against a deliberately broken expectation, and the stress test is run
against a bootloader that is known to be unsafe.

It holds two benches:

- **F407 bench** (`bench.py`): functional tests for an STM32F407G-DISC1. Test plans are TOML files, and
  the tool recovers the board by itself when a test leaves it hung or crashed.
- **Bootloader stress test** (`python -m stress`): an A/B bootloader for the NUCLEO-C031C6 that is
  reset at thousands of chosen moments during a firmware update. An ESP32-S3 pulls the reset line with
  microsecond timing. A deliberately unsafe bootloader is tested the same way as a control.

<p align="center">
  <img src="docs/egitim/foto/genel.jpg" width="360"
       alt="The C031 bench: an ESP32-S3 wired to a NUCLEO-C031C6 with four jumper wires">
</p>
<p align="center"><em>The C031 bench: the ESP32-S3 bridge (top) and the NUCLEO-C031C6.</em></p>

**Contents:** [Results](#results-so-far) ·
[Try it without hardware](#try-it-without-hardware) ·
[Requirements](#requirements) ·
[Bootloader stress test](#the-bootloader-stress-test) ·
[F407 bench](#the-f407-bench) ·
[Using it with an agent](#using-it-with-an-agent-claude-code-opencode) ·
[What is in the repo](#what-is-in-the-repo) ·
[Development](#development)

## Results so far

| Bench | Result |
|---|---|
| NUCLEO-C031C6, A/B bootloader | 0 failures in 3103 updates that were reset mid-update (2026-11-07). The failure rate is below 0.097% with 95% confidence. |
| NUCLEO-C031C6, naive bootloader (control) | 11 of 20 reset-injected updates booted a corrupt image, as the control is meant to show |
| F407 Discovery | `smoke`, `crash_recovery`, `measure` and `camera_blink` pass on the real board, each with its negative check (2026-10-28) |

The F407 port of the stress test (`firmware/boot/`) has only run in the simulator so far. The full
numbers are in [`stress/README.md`](stress/README.md), and the slide deck is
[`docs/stress/sunum.html`](docs/stress/sunum.html) (Turkish; GitHub shows its source, so download it
and open it in a browser).

## Try it without hardware

You need [uv](https://docs.astral.sh/uv/). It fetches the right Python and the packages on the first
`uv run`. Nothing else is required for these commands.

```bash
git clone https://github.com/aarmert/tezgah-lab.git tezgah && cd tezgah

# Stress test against the simulator: bootloader, flash and reset behaviour are all modelled
uv run python -m stress --json plan reset_sweep --seed 1 --sim --target c031    # the faults to inject
uv run python -m stress --json run reset_sweep --seed 1 --sim --target c031     # 288/288 trials pass
uv run python -m stress --json run naive_control --seed 1 --sim --target c031   # the control: 47/60 unsafe

# Host-side unit tests
uv run python -m unittest discover -s stress/tests
uv run python -m unittest discover -s firmware/boot/tests
uv run python -m unittest discover -s firmware/boot_c0/tests
```

Without `--target c031` the simulator models the F407 (255 trials in `reset_sweep`).

`bench.py` has a simulated board too. It needs no `bench.toml`, but a plan flashes the firmware, so
build it first (this needs `arm-none-eabi-gcc`):

```bash
uv run firmware/build.py
uv run bench.py --sim run crash_recovery
uv run bench.py --json --sim send PING --expect '^PANG$'    # a wrong expectation, to see a failure
```

The last command fails on purpose. This is what a failed result looks like:

```json
{
  "ok": false,
  "command": "send",
  "error_type": "expect_timeout",
  "summary": "No /^PANG$/ reply to 'PING' within 2 s.",
  "likely_causes": [
    "The board may have crashed or hung earlier: try 'bench.py reset --expect \"^BOOT\"'",
    "TX/RX wires swapped, or GND not connected"
  ],
  "details": "Recent serial traffic:\n> PING\n< PONG",
  "audit_hash": "9ec01c6f8857e54c1c877dc1b433fa8ea7222d24836258981834aea40173241d"
}
```

## Requirements

| For | You need |
|---|---|
| Everything | [uv](https://docs.astral.sh/uv/) (installs Python 3.14 and `pyserial`) |
| Flashing and debugging | OpenOCD, or STM32CubeProgrammer (`bench.py` only). The C031 needs an OpenOCD build newer than 0.12.0, which has no `stm32c0x.cfg`. |
| Building firmware | `arm-none-eabi-gcc` |
| Camera steps | `adb`, scrcpy 2.5+, `ffmpeg`, [Bun](https://bun.sh), the `camera` group (opencv, numpy; `uv sync` installs it), and an Android 12+ phone with USB debugging |
| ESP32 bridge | `arduino-cli` with the esp32 core |
| `report --plot` | matplotlib (the `plot` group) |

The Python packages are declared in `pyproject.toml` and pinned in `uv.lock`. `uv sync` creates
`.venv` with all of them (pyserial, plus the `camera`, `plot`, `docs`, `esp32`, and `dev` groups).

Config files: the F407 bench reads `bench.toml`, and the C031 bench reads `bench-c031.toml` and
`stress.toml`. Each has an `.example` template to copy.

## The bootloader stress test

The question it answers: if power fails or the board resets at any moment of a firmware update, does
the board still boot a correct image?

- **The bootloader under test** keeps two slots, A and B. A new image goes to the inactive slot and boots
  as a trial. The app must confirm itself within about a second, otherwise the watchdog resets it and
  after three attempts the bootloader rolls back. An append-only log in flash records each step, and
  every image is checked with CRC32 on every boot.
- **The control** is a naive bootloader that overwrites the only slot in place. The same tests must
  show it failing, which proves the tests can detect a failure.
- **Fault injection** has two sources. A hook in the test builds resets the chip from a timer at a
  chosen phase and delay. The ESP32-S3 pulls NRST from outside, a set number of microseconds after
  the last byte of a chosen line (measured 2 to 15 µs late).
- **The verdict** does not trust the board. After each trial the host reads both slots and the log
  back over SWD and classifies the outcome from the flash contents and the serial lines.

### Outcomes

The host classifies each trial as one of:

| Outcome | Meaning |
|---|---|
| `NEW` | The new image runs |
| `OLD` | The old image still runs |
| `PENDING` | The bootloader waits for the update to resume |
| `DEAD` | Nothing boots |
| `WRONG` | A corrupt image was started |
| `UNCLEAR` | Unclear |

The first three are accepted. `PENDING` counts only if the resumed update then succeeds. Results
written before 2026-11-10 use the Turkish names (`YENİ`, `ESKİ`, `BEKLİYOR`, `ÖLÜ`, `YANLIŞ`,
`BELİRSİZ`); `report` still reads them and maps them to the English names.

### Wiring (C031 bench)

```
PC USB ── NUCLEO-C031C6 ST-LINK/V2-1     SWD + virtual COM port (USART2 PA2/PA3, 115200)
PC USB ── ESP32-S3 UART connector        CH343, 921600

ESP32 GPIO17 ──────────────────►  PA10 (USART1 RX)
ESP32 GPIO18 ◄──────────────────  PA9  (USART1 TX)
ESP32 GPIO4 (open-drain) ──────►  NRST
GND ────────────────────────────  GND
3V3 and 5V are not connected between the boards.
```

### Running it

```bash
cp stress.example.toml stress.toml                 # then fill in the ports
cp bench-c031.example.toml bench-c031.toml
uv run firmware/boot_c0/build_boot.py --json      # bootloaders, apps, images
uv run esp32/build.py --json                      # compile the bridge, then upload it yourself

uv run python -m stress --json doctor --target c031
uv run python -m stress --json calibrate --target c031
uv run python -m stress --json run basic --target c031 --n 20
uv run python -m stress --json run reset_sweep --target c031 --reset-source esp
uv run python -m stress --json report stress_runs/<run-folder> --plot
```

The tests are `basic`, `transfer_cut`, `line_noise`, `reset_random`, `reset_sweep`, `double_fault`,
`bad_image`, `rollback` and `naive_control`. [`stress/README.md`](stress/README.md) describes each one,
how a trial runs, how outcomes are classified and how to read the results.

## The F407 bench

### Wiring

```
Discovery mini-USB (ST-Link)  ->  computer USB   (flashing and reset)

USB-TTL (3.3V)                    Discovery
  RX   <----------------------    PA2  (USART2 TX)
  TX   ---------------------->    PA3  (USART2 RX)
  GND  -----------------------    GND
  VCC  do not connect! (the board is powered over USB)
```

Make sure the USB-TTL adapter is in 3.3V mode. If it has a jumper, set it to 3V3.
The CN3 jumpers on the Discovery must be fitted (ST-Link connected to the board).

### Ubuntu setup

```bash
bash setup_ubuntu.sh
# log out and back in, then:
uv run bench.py ports        # find the USB-TTL's name and write it to bench.toml
uv run bench.py doctor
uv run bench.py run smoke
uv run bench.py run crash_recovery
```

The script installs [uv](https://docs.astral.sh/uv/) (unless it's already installed) and runs
`uv sync --locked`. uv uses the Python version in `.python-version`, downloading it if the system
doesn't have it, and installs the packages at the versions in `uv.lock` into `.venv`.

Read `setup_ubuntu.sh` before you run it. Besides installing packages with `sudo` and adding you to the
`dialout` and `plugdev` groups, it stops the machine from suspending (it masks the sleep targets and
ignores the lid switch). That suits a dedicated bench computer and probably not your laptop. It does
not install the camera tools (`adb`, scrcpy 2.5+, `ffmpeg`, Bun). Install those yourself and run
`bun install` once in the repo root; see [`camera/README.md`](camera/README.md).

Use the `/dev/serial/by-id/...` name from the `bench.py ports` output. After a reboot, `/dev/ttyUSB0`
can point to a different device. The by-id name stays the same.

### Windows setup

1. Install [uv](https://docs.astral.sh/uv/) and run `uv sync` in the repo.
2. Install STM32CubeProgrammer (it includes the ST-Link driver). For the compiler, install STM32CubeCLT or the Arm GNU Toolchain.
3. Copy `bench.example.toml` to `bench.toml`. Set `backend = "cubeprog"` and `port = "COM5"` (find the port in Device Manager or with `uv run bench.py ports`).
4. `uv run firmware\build.py`, `uv run bench.py doctor`, `uv run bench.py run smoke`

`halt-dump`, `mem`, and `gdbserver` are not available with the `cubeprog` backend.

### Commands

| Command | What it does |
|---|---|
| `uv run bench.py doctor` | Checks the tools, the ST-Link, and the serial port |
| `uv run bench.py ports` | Lists serial ports |
| `uv run bench.py flash file.elf` | Flashes and verifies |
| `uv run bench.py reset --expect "^BOOT"` | Resets and waits for the boot message |
| `uv run bench.py send PING --expect "^PONG$"` | Sends a command and checks the reply |
| `uv run bench.py send TICK --expect "^TICK (\d+)$" --min 1 --max 100000` | Checks a numeric range |
| `uv run bench.py read --seconds 3` | Reads from the serial port |
| `uv run bench.py monitor` | Live serial monitor (Ctrl+C to quit) |
| `uv run bench.py run smoke` | Runs the `plans/smoke.toml` plan |
| `uv run bench.py run smoke --verify-can-fail` | Also runs a deliberately broken copy of the plan, which must FAIL |
| `uv run bench.py recover` | Recovers the board by flashing the known-good firmware under reset |
| `uv run bench.py audit` | Checks that nobody has altered the audit log |
| `uv run bench.py station` | Production station: asks for the operator name, runs smoke+measure on each board, shows a large PASS/FAIL, `q` to quit |
| `uv run bench.py halt-dump` | Halts the core, shows the registers, the fault registers with descriptions, and the pc and stack at the crash, then resumes |
| `uv run bench.py mem 0x40020C14 --count 4` | Reads memory or peripheral registers (read only, the option byte region is blocked) |
| `uv run bench.py gdbserver` | Starts a GDB server (for humans, Ctrl+C to stop; `sudo apt install gdb-multiarch`) |

`halt-dump`, `mem`, and `gdbserver` work only with `backend = "openocd"`, and `halt-dump` and `mem`
have F407 constants, so they give wrong results on the C031.
Reading some peripheral registers has side effects. For example, reading the USART `DR` consumes the received character.

To get the result as JSON (`ok`, `error_type`, `summary`, `likely_causes`), put `--json` before the
subcommand: `uv run bench.py --json run smoke`. `--sim` goes in the same place and runs the command
without hardware. After the subcommand, `bench.py` rejects both.

Each `run` creates a `runs/<YYYYmmdd-HHMMSS>_<plan>/` folder with `report.md`, `result.json`,
and a timestamped `serial.log`. If a test fails, the tool tries to recover the board itself. It resets
the board first, and if that does not work, it flashes the known-good firmware again.
If the board crashes, the tool maps the address in the HARDFAULT line to a function and line in the source code.

### Writing a test plan

The plans in `plans/` are working examples; [`plans/smoke.toml`](plans/smoke.toml) is the shortest.
The steps below and `cam_blink` in the next section are all eight actions.

```toml
name = "example"
firmware = "firmware/build/stm32bench-fw.elf"

[[step]]
action = "flash"            # flash the plan's firmware

[[step]]
action = "reset"

[[step]]
action = "expect"           # wait for a line that matches this pattern
pattern = '^BOOT '
timeout = 3

[[step]]
action = "send"             # send and check the reply
text = "TICK"
expect = '^TICK (\d+)$'
range = [1, 600000]         # the number in the capture group must be in this range

[[step]]
action = "silence"          # no output may arrive during this time
seconds = 1

[[step]]
action = "delay"
seconds = 0.5

[[step]]
action = "clock_check"      # TICK, wait 5 s on the PC, TICK again
seconds = 5
range = [0.97, 1.03]        # board time / PC time (time.monotonic) must be in this range
```

Patterns are regular expressions. Use single quotes in TOML so you don't need to escape backslashes.

`clock_check` writes the ratio, both durations, and the uncertainty of the PC measurement (half the TICK
round trip). If the last expectation step is a `clock_check`, `--verify-can-fail` breaks its range.

Every new plan should pass `--verify-can-fail`. A test that does not fail when the expectation is
deliberately broken proves nothing.

### Measuring an LED with the camera (`cam_blink`)

```toml
[[step]]
action = "cam_blink"        # record a video with the phone and measure the LED's blink frequency
seconds = 4                 # recording length
range = [1.8, 2.2]          # Hz
# duty_range = [0.4, 0.6]   # optional duty cycle range
# skip_s = 0.5              # skip the first frames (let exposure and focus settle)

[[step]]
action = "cam_blink"        # is the LED steady? The region and threshold come from the previous cam_blink step
seconds = 3
steady = "on"               # or "off"
```

- The `camera/` tool (`camera/scripts/record-video.ts`, scrcpy) records the video. Frame times come from
  the video's own timestamps (ffprobe).
- The LED region is the 30×30 pixel spot whose brightness changes most across the frames. If the LED's
  reflection on a shiny surface changes more, the tool picks the reflection. The frequency comes out the same,
  but check the marked frame. To fix the region, add `[camera] roi = [x, y, width, height]` to `bench.toml`.
- The verdict is numeric. The threshold is the midpoint between the 5th and 95th brightness percentiles, transitions
  use hysteresis, and the tool finds each transition time by linear interpolation between two frames.
  It computes the frequency from both rising and falling edges.
- If the measured frequency is above a quarter of the frame rate, the message starts with `WARNING:`.
  Frequencies above half the frame rate alias, so they look lower than they are.
- Each step writes `cam_stepNN.mp4`, `cam_stepNN_roi.jpg` (region marked, other candidates as yellow
  circles), and `cam_stepNN.csv` (time, brightness) to the report folder.
- Run `bun install` once in the repo root. With `--sim`, the tool uses a synthetic video.
- If Bun is not at `~/.bun/bin/bun`, or more than one phone is attached, set `bun` or `serial` under
  `[camera]` in `bench.toml`. `bench.example.toml` lists the keys.

### Production station (`station`)

```bash
uv run bench.py station                              # asks for the operator, waits for Enter on each board, q to quit
uv run bench.py station --plans smoke,measure,camera_blink
uv run bench.py station --rounds 3 --no-prompt --operator Ayse   # for testing and automation
```

- For each board, the plans run in order. The station stops at the first failing plan and shows the result as a large PASS/FAIL.
- For each board, the station writes a `station-card` line to the audit log (`runs/audit.jsonl`). The hash
  of that line goes into the header of every report for that board. The header also has the operator, UID,
  firmware SHA-256, bench.py and OpenOCD versions, host name, and start and end times.
- `runs/station_<date>.csv` has one row per board. The columns are `time`, `board_no`, `operator`, `uid`,
  `firmware_sha256`, `result`, `failed_step`, `report_dirs`, `repeat`, and `audit_hash`. Files written before
  the columns were renamed have Turkish headers; they are still read for the repeat check.
- If the same UID has been tested before, the screen shows a `!!!` warning that the board was tested before,
  and the CSV's `repeat` column is filled in. The check covers all `station_*.csv` files.
- Runs with `--sim` go to `station_<date>_sim.csv`, so they never mix with the real log.
- While the station is open, it holds the bench lock. Other bench.py commands wait until it closes.

### Test firmware commands

`PING`, `VER`, `TICK`, `UID`, `VDD` (supply voltage in mV), `TEMP` (chip temperature in °C, 0.1 resolution),
`LED <G|O|R|B> <ON|OFF>`, `BLINK <1-20>`
(the green LED's blink rate, for the camera test), `CRASH` (a deliberate HardFault),
`RESET`. At boot it prints `BOOT stm32bench-fw v1.2.0 reset=<cause>`. The green LED
blinks at 2 Hz while the main loop runs, and stops if the board hangs.

The firmware targets the STM32F407G-DISC1 and STM32F411E-DISCO (LEDs on PD12-PD15). `TEMP` is correct
only on the F407, because the F411's temperature sensor is on ADC channel 18, not 16.
The clock is the internal 16 MHz HSI, with no PLL setup.

### Common errors

| Error | Fix |
|---|---|
| `probe_not_found` | Check the USB cable and the `plugdev` group (log out and back in), or another program is holding the ST-Link |
| `target_not_responding` | `uv run bench.py recover` |
| `serial_open_failed` | Wrong port name, or you are not in the `dialout` group |
| `expect_timeout` with no lines at all | TX/RX swapped, GND not connected, or the USB-TTL is in 5V mode |
| Garbled characters | Baud rate mismatch (it must be 115200) |

## Using it with an agent (Claude Code, OpenCode)

Start `claude` or `opencode` in this folder. The rules are in `AGENTS.md`, and Claude reads it through
`CLAUDE.md`. The agent touches the board only through `bench.py` and `python -m stress`, cannot change the
test files, and does not touch option bytes. `AGENTS.md` has an `<ADB_SERIAL>` placeholder for the
phone; replace it with your phone's serial from `adb devices`.

`AGENTS.md` only asks the agent to leave the tests alone. Two settings enforce part of it:

```bash
chmod -R a-w plans bench.toml bench.py      # Linux and macOS; the agent can no longer edit them
```

and `plans_dir_only = true` under `[permissions]` in `bench.toml`, which makes `run` refuse any plan
file outside `plans/`. Neither is a security boundary against an agent that runs as your user and
decides to undo them.

## What is in the repo

| Path | Purpose |
|---|---|
| `bench.py` | The F407 bench tool: flash, reset, send and read over serial, run test plans, recover |
| `bench.example.toml`, `bench-c031.example.toml`, `stress.example.toml` | Config templates. Copy each one without `.example`; the copies are gitignored. |
| `plans/` | Test plans: `smoke`, `crash_recovery`, `measure`, `camera_blink` |
| `firmware/` | The F407 test firmware (`main.c`) and `build.py`. Bare metal, no HAL, no make. |
| `firmware/boot_c0/`, `firmware/boot/` | Stress-test bootloaders and apps for the C031 and the F407 ([C031](firmware/boot_c0/README.md), [F407](firmware/boot/README.md)) |
| `firmware/wiretest_c031/` | A small wiring check for the C031 bench. Not part of the stress system. |
| `stress/` | The stress-test host tool and its simulator ([`stress/README.md`](stress/README.md)) |
| `esp32/bridge/` | ESP32-S3 serial bridge with a timed NRST pulse ([README](esp32/bridge/README.md)) |
| `camera/` | The phone camera tool: Bun, TypeScript, scrcpy ([`camera/README.md`](camera/README.md)) |
| `.opencode/plugins/android-camera/` | The `android_take_photo` tool for OpenCode |
| `docs/stress/` | The stress-test specs: [`SPEC.md`](docs/stress/SPEC.md), [`SPEC-C031C6.md`](docs/stress/SPEC-C031C6.md), [`SPEC-ESP32.md`](docs/stress/SPEC-ESP32.md) |
| `docs/egitim/` | Generators for the Turkish training booklet and the 3000-trial report, and the bench photos. The PDFs are built locally and are not committed. |
| `setup_ubuntu.sh` | Installs the F407 bench's dependencies on Ubuntu |
| `AGENTS.md` | The rules a coding agent follows in this folder (`CLAUDE.md` imports it) |

### Language

The code, the READMEs, the specs and the tools' output are in English. The training booklet, the
3000-trial report and the slide deck are Turkish.

## Development

```bash
uv run python -m unittest discover -s stress/tests             # 140 tests
uv run python -m unittest discover -s firmware/boot/tests      # 30 tests
uv run python -m unittest discover -s firmware/boot_c0/tests   # 42 tests
bun install && bun run test && bun run typecheck         # camera tool, 19 tests
```

None of these need hardware. The camera tests use fake `adb` and `scrcpy`. Lint with
`uv run ruff check .`.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
