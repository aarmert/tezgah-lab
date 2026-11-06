# tezgah

A tool that flashes and resets an STM32F4 Discovery board from a computer, talks to it over a serial
port, watches its LEDs with an Android phone camera, and runs test plans. You and Claude (with `--json`)
can both use it. It runs the same way on Linux and Windows.

## Contents

| File | Purpose |
|---|---|
| `bench.py` | The main tool: flash, reset, send and read over serial, run test plans, recover |
| `bench.example.toml` | Config template (copy it to `bench.toml`) |
| `plans/smoke.toml` | Basic test: flash, boot message, PING/VER/TICK/UID/LED |
| `plans/crash_recovery.toml` | Crashes the board on purpose, reads the report, brings it back with a reset |
| `plans/measure.toml` | Internal measurements: VDD (VREFINT), chip temperature, board clock to PC clock ratio |
| `plans/camera_blink.toml` | Uses the phone camera to measure the green LED's frequency (BLINK 1/2/4) and check that it stays lit with LED G ON |
| `firmware/` | The small test firmware that runs on the board, and `build.py` (no make needed) |
| `camera/` | The phone camera tool (Bun + TypeScript, scrcpy). Details: [`camera/README.md`](camera/README.md) |
| `.opencode/plugins/android-camera/` | The `android_take_photo` tool for OpenCode |
| `docs/raporlar/` | Trial reports |
| `setup_ubuntu.sh` | Script that installs everything on Ubuntu |
| `AGENTS.md` | Rules agents follow in this folder (`CLAUDE.md` imports it) |

## Wiring

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

## Ubuntu setup

```bash
# Copy from Windows to the server (use your own user name and IP)
scp tezgah.zip user@server-ip:~
ssh user@server-ip
sudo apt install -y unzip && unzip tezgah.zip && cd tezgah
bash setup_ubuntu.sh
# log out and back in, then:
python3 bench.py ports        # find the USB-TTL's name and write it to bench.toml
python3 bench.py doctor
python3 bench.py run smoke
python3 bench.py run crash_recovery
```

Use the `/dev/serial/by-id/...` name from the `bench.py ports` output. After a reboot, `/dev/ttyUSB0`
can point to a different device. The by-id name stays the same.

## Commands

| Command | What it does |
|---|---|
| `python3 bench.py doctor` | Checks the tools, the ST-Link, and the serial port |
| `python3 bench.py ports` | Lists serial ports |
| `python3 bench.py flash file.elf` | Flashes and verifies |
| `python3 bench.py reset --expect "^BOOT"` | Resets and waits for the boot message |
| `python3 bench.py send PING --expect "^PONG$"` | Sends a command and checks the reply |
| `python3 bench.py send TICK --expect "^TICK (\d+)$" --min 1 --max 100000` | Checks a numeric range |
| `python3 bench.py read --seconds 3` | Reads from the serial port |
| `python3 bench.py monitor` | Live serial monitor (Ctrl+C to quit) |
| `python3 bench.py run smoke` | Runs the `plans/smoke.toml` plan |
| `python3 bench.py run smoke --verify-can-fail` | Also runs a deliberately broken copy of the plan, which must FAIL |
| `python3 bench.py recover` | Recovers the board by flashing the known-good firmware under reset |
| `python3 bench.py audit` | Checks that nobody has altered the audit log |
| `python3 bench.py station` | Production station: asks for the operator name, runs smoke+measure on each board, shows a large PASS/FAIL, `q` to quit |
| `python3 bench.py halt-dump` | Halts the core, shows the registers, the fault registers with descriptions, and the pc and stack at the crash, then resumes |
| `python3 bench.py mem 0x40020C14 --count 4` | Reads memory or peripheral registers (read only, the option byte region is blocked) |
| `python3 bench.py gdbserver` | Starts a GDB server (for humans, Ctrl+C to stop; `sudo apt install gdb-multiarch`) |

For now, `halt-dump`, `mem`, and `gdbserver` work only with `backend = "openocd"`.
Reading some peripheral registers has side effects. For example, reading the USART `DR` consumes the received character.

Add `--json` to any command to get the result as JSON (`ok`, `error_type`,
`summary`, `likely_causes`). Add `--sim` to try it without hardware.

Each `run` creates a `runs/<date>_<plan>/` folder with `report.md`, `result.json`,
and a timestamped `serial.log`. If a test fails, the tool tries to recover the board itself. It resets
the board first, and if that does not work, it flashes the known-good firmware again.
If the board crashes, the tool maps the address in the HARDFAULT line to a function and line in the source code.

## Production station (`station`)

```bash
python3 bench.py station                              # asks for the operator, waits for Enter on each board, q to quit
python3 bench.py station --plans smoke,measure,camera_blink
python3 bench.py station --rounds 3 --no-prompt --operator Ayse   # for testing and automation
```

- For each board, the plans run in order. The station stops at the first failing plan and shows the result as a large PASS/FAIL.
- For each board, the station writes a `station-card` line to the audit log (`runs/audit.jsonl`). The hash
  of that line goes into the header of every report for that board. The header also has the operator, UID,
  firmware SHA-256, bench.py and OpenOCD versions, host name, and start and end times.
- `runs/station_<date>.csv` has one row per board. The columns are zaman (time), kart_no (board number),
  operator, uid, firmware_sha256, sonuc (result), basarisiz_adim (failed step), rapor_klasoru (report folder),
  tekrar (repeat), and denetim_hash (audit hash).
- If the same UID has been tested before, the screen shows `!!! BU KART DAHA ÖNCE TEST EDİLDİ` ("this board
  was tested before") and the CSV's `tekrar` column is filled in. The check covers all `station_*.csv` files.
- Runs with `--sim` go to `station_<date>_sim.csv`, so they never mix with the real log.
- While the station is open, it holds the bench lock. Other bench.py commands wait until it closes.

## Writing a test plan

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

`clock_check` writes the ratio, both durations, and the uncertainty of the PC measurement (half the TICK
round trip). If the last expectation step is a `clock_check`, `--verify-can-fail` breaks its range.

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
- If the measured frequency is above a quarter of the frame rate, the message starts with `UYARI:` ("warning").
  Frequencies above half the frame rate alias, so they look lower than they are.
- Each step writes `cam_stepNN.mp4`, `cam_stepNN_roi.jpg` (region marked, other candidates as yellow
  circles), and `cam_stepNN.csv` (time, brightness) to the report folder.
- It needs OpenCV and NumPy: `sudo apt install python3-opencv python3-numpy`. The camera tool needs
  adb, scrcpy 2.5+, ffmpeg, and [Bun](https://bun.sh). Run `bun install` once in the root. With `--sim`, the tool uses a synthetic video.

Patterns are regular expressions. Use single quotes in TOML so you don't need to escape backslashes.

## Test firmware commands

`PING`, `VER`, `TICK`, `UID`, `VDD` (supply voltage in mV), `TEMP` (chip temperature in °C, 0.1 resolution),
`LED <G|O|R|B> <ON|OFF>`, `BLINK <1-20>`
(the green LED's blink rate, for the camera test), `CRASH` (a deliberate HardFault),
`RESET`. At boot it prints `BOOT stm32bench-fw v1.2.0 reset=<cause>`. The green LED
blinks at 2 Hz while the main loop runs, and stops if the board hangs.

The firmware targets the STM32F407G-DISC1 and STM32F411E-DISCO (LEDs on PD12-PD15). `TEMP` is correct
only on the F407, because the F411's temperature sensor is on ADC channel 18, not 16.
The clock is the internal 16 MHz HSI, with no PLL setup.

## Using it with an agent (Claude, OpenCode)

Start `claude` or `opencode` in this folder. The rules are in `AGENTS.md`, and Claude reads it through
`CLAUDE.md`. The agent touches the board only through `bench.py`, cannot change the test files, and does not touch option bytes.

To make sure Claude cannot change the tests:

```bash
chmod -R a-w plans bench.toml bench.py
```

and set `plans_dir_only = true` in `bench.toml`.

## On Windows

1. Install Python 3.11+ from python.org (with "Add to PATH" checked).
2. `py -m pip install pyserial`
3. Install STM32CubeProgrammer (it includes the ST-Link driver). For the compiler, install STM32CubeCLT or the Arm GNU Toolchain.
4. In `bench.toml`, set `backend = "cubeprog"` and `port = "COM5"` (find the port in Device Manager or with `py bench.py ports`).
5. `py firmware\build.py`, `py bench.py doctor`, `py bench.py run smoke`

## Common errors

| Error | Fix |
|---|---|
| `probe_not_found` | Check the USB cable and the `plugdev` group (log out and back in), or another program is holding the ST-Link |
| `target_not_responding` | `python3 bench.py recover` |
| `serial_open_failed` | Wrong port name, or you are not in the `dialout` group |
| `expect_timeout` with no lines at all | TX/RX swapped, GND not connected, or the USB-TTL is in 5V mode |
| Garbled characters | Baud rate mismatch (it must be 115200) |

## Status

The firmware has run in the QEMU emulator, and the tool has run against a simulated board and over a real
serial port. Its first run on a real Discovery board will be with you, so check the result of the first `run smoke`.
