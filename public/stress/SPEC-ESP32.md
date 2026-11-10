# Bootloader stress test: ESP32-S3 bridge + two-UART layout (addendum, v0.1.1)

This applies to the **NUCLEO-C031C6 + ESP32-S3** bench (`SPEC-C031C6.md`). Everything else from
`SPEC.md` and `SPEC-C031C6.md` stays valid. The wiring is verified (2026-11-07, `firmware/wiretest_c031/`).

```
PC ──USB── ESP32-S3 (CH343 UART0, /dev/serial/by-id/usb-1a86_USB_Single_Serial_*)   921600 8N1
PC ──USB── NUCLEO ST-LINK: SWD + VCP (/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_*) 115200
ESP32 GPIO17 (UART1 TX) ──► C031 PA10 (USART1 RX)
ESP32 GPIO18 (UART1 RX) ◄── C031 PA9  (USART1 TX)
ESP32 GPIO4  (open-drain) ─► C031 NRST
GND ── GND
```

## 1. Two UARTs on the C031 (firmware rule, BL and apps)

- USART1 (PA9 TX / PA10 RX, AF1) is the **update channel** to the ESP32. USART2 (PA2/PA3, VCP) is the
  **witness channel** to the PC. Both run at 115200 8N1.
- Enable the PA10 internal pull-up: while the ESP32 is not driving its TX (e.g. booting), a floating
  line reads as a stream of 0x00 bytes (measured).
- **Every line the firmware prints goes to both UARTs**, in the same order (status lines, replies,
  `T` trace lines, banners, `APP confirmed`, `HARDFAULT`, `BL fault_*`).
- **Commands are accepted on both UARTs**, with a separate line buffer per UART. A line is processed
  when it is complete; replies go to both UARTs. Bytes from one UART must never be mixed into the
  other's line buffer. A line that overflows or contains a NUL byte is dropped and answered with
  `ERR LINE` (in update mode) or `ERR unknown command` (app), as in SPEC.md.
- The BL's update-mode inactivity timer is reset by a valid command from either UART.

## 2. ESP32-S3 bridge firmware (`esp32/bridge/`, Arduino-ESP32, built with arduino-cli)

The ESP32 is a **line bridge with a precisely timed NRST**. The update logic (stop-and-wait,
retries, noise injection) stays in the Python host, so the same `stress/updater.py` drives both
benches. The ESP32 adds the physical path (its own UART to the STM32) and the external reset.

UART0 (USB, PC side) at 921600. UART1 to the STM32 at 115200, TX=GPIO17, RX=GPIO18, configured as
early as possible in `setup()` so TX idles high. GPIO4 is open-drain, released (high) by default.

### PC → ESP32 (one command per line, `\n` terminated)
| Command | Effect | Reply |
|---|---|---|
| `PING` | | `ESP PONG tezgah-bridge 0.1` |
| `TX <text>` | send `<text>\r\n` to the STM32 | none (errors: `ESP ERR ...`) |
| `TXH <hex>` | send raw bytes (hex-encoded, no CR/LF added) to the STM32 | none |
| `NRST PULSE <us>` | pull NRST low for `<us>` (10..1000000), then release | `ESP OK PULSE` |
| `NRST ARM TX <delay_us> [<n>]` | fire a 1 ms NRST pulse `<delay_us>` after the **last byte of the n-th following TX/TXH** has left the UART (n default 1) | `ESP OK ARM` |
| `NRST ARM RX <delay_us> <prefix>` | fire `<delay_us>` after a received STM32 line starting with `<prefix>` is complete | `ESP OK ARM` |
| `NRST DISARM` | cancel | `ESP OK DISARM` |
| `STAT` | | `ESP STAT rx_lines=.. tx_lines=.. fired=.. overruns=..` |

- Timing: use the UART "TX done" event / `uart_wait_tx_done` (or `Serial1.flush()`, which waits for
  the FIFO to drain) and then a one-shot `esp_timer` (µs resolution). Measure the jitter in a
  self-test and print it: `ESP ARMJITTER <us>` after each fire, the difference between the planned
  and the actual moment of the NRST edge, as read back from the GPIO.
- When the NRST pulse fires: `ESP NRST FIRED <delay_us> <late_us>`.
- Only one arm at a time; a new ARM replaces the old one. `NRST PULSE` leaves a pending ARM in place.

### 2.1 Details fixed by the implementation (`esp32/bridge/bridge.ino`, v0.1.1)
- `RX` escaping also covers the backslash (`\` → `\x5C`), so `\xHH` is unambiguous. A received line
  longer than 1024 bytes is passed on in 1024-byte `RX` pieces. GPIO18 has its pull-up enabled.
- PC lines longer than 2100 chars → `ESP ERR LINE`. Errors: `ESP ERR TXH` (empty/odd/non-hex),
  `ESP ERR PULSE` (range), `ESP ERR ARM` (syntax), `ESP ERR unknown command`. `TX` with no text sends
  only CR LF. `ESP OK PULSE` comes after the release.
- Timing: UART1 has no TX ring buffer, so `write()` returns when every byte is in the FIFO and
  `flush()` spins until the transmitter is idle (the end of the last stop bit). The pulse is planned
  at that moment + delay; the last 300 µs of every delay are busy-waited (an `esp_timer` one-shot
  wakes the bridge for longer delays). `late_us` = the falling edge read back from the pad − the plan;
  `ESP ARMJITTER <late_us>` follows every `ESP NRST FIRED`. ARM RX timestamps the `\n` when the main
  loop reads it (RX FIFO threshold 1).
- Extensions (not needed by the protocol above, used by the host):
  - `TIME ON|OFF` → `ESP OK TIME`. While on, every TX/TXH waits for the stop bit and is followed by
    `ESP TXDONE <tx_count> <t_us>`, and every `RX` line is preceded by `ESP RXT <t_us>` (the moment
    its `\n` was read). `t_us` is the ESP's `esp_timer` clock. `calibrate` over the bridge uses it.
  - `SELFTEST` → `ESP SELFTEST n=20 late_us min=.. avg=.. max=..`: timer wake-up lateness for 20
    delays from 50 µs to 4.8 ms. It never touches NRST.
- Build: `python3 esp32/build.py --json` (arduino-cli, FQBN
  `esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=default,PartitionScheme=app3M_fat9M_16MB`).
  Upload is a human step (`esp32/bridge/README.md`).

### ESP32 → PC
| Line | Meaning |
|---|---|
| `RX <text>` | one complete line received from the STM32 (CR stripped; bytes < 0x20 or > 0x7E shown as `\xHH`) |
| `ESP READY tezgah-bridge 0.1` | after boot |
| `ESP OK ...` / `ESP ERR ...` / `ESP NRST FIRED ...` / `ESP STAT ...` | ESP's own messages |

## 3. Host (`stress/`)

- `--target c031` (default `f407`) selects `layout_c031`, the image folder, the bench config and
  the timings from `SPEC-C031C6.md`.
- `--link esp|vcp` (default `esp` for c031):
  - `esp`: commands and the update go through the ESP32 bridge (`stress/esp_bridge.py`: a transport
    with the bench interface `write(bytes)`, `read_available(timeout) -> str`, `close()` that wraps
    each line in `TX ...` (`TXH` for injected raw/corrupted bytes) and unwraps `RX ...`). The VCP is
    opened in parallel as the **witness** and logged to `serial.log` with a `V` direction marker.
  - `vcp`: everything over the VCP (like the F407), for debugging without the ESP32.
- Classification uses the line stream of the chosen link. In `esp` mode the host also checks that
  the witness saw the same status/banner lines; a mismatch is recorded in `note` (`witness_mismatch`).
- `--reset-source hook|esp` for reset_random / reset_sweep / naive_control:
  - `hook`: the in-target fault hook (SPEC §6), µs precise, phase-aware.
  - `esp`: an external NRST pulse from the ESP32, armed on the TX of the k-th protocol line
    (BEGIN = phase ERASE, DATA k, END = HDR/META) with a delay. This is the "real pin" reset. The
    CSV `fault_type` is `nrst_esp`, `phase`/`phase_index` name the trigger line, `note` holds
    `late_us`.
  - The host arms right before the trigger line (`NRST ARM TX <delay> 1`, then the line), not k
    lines ahead, so a retried line cannot shift the count. Trigger points: `ERASE -1` = `UPD BEGIN`,
    `DATA k` = the first transmission of chunk k, `HDR 0` (and `META`) = `UPD END`. The pulse fires
    once; at the end of the trial the host waits for a pending `ESP NRST FIRED`, then sends
    `NRST DISARM`. Notes: `reset_source=esp`, `late_us=<n>` per pulse, `esp_not_fired` /
    `esp_not_armed`. `fault_fired` counts the pulses. Each fired pulse is an explained reset in the
    SPEC §7 boot count (`nrst`). The BL needs no hooks for this (entry 0 is not sent as FAULT ARM).
  - Delay ranges come from `calibrate` over the bridge with `TIME ON`: per trigger line, the time from
    its last byte leaving the ESP to the end of the reply (`OK ERASED`, `OK <off>`; for `UPD END` the
    `APP confirmed` after the trial boot, or the app banner when nothing confirms, e.g. naive), in ESP
    microseconds, max over the runs. reset_random draws from `UPD BEGIN` → `OK COMMIT`. The sweep is
    `ERASE -1`, `DATA k` (k below the common chunk count), `HDR 0`, capped per phase as in SPEC §8.
  - `double_fault` keeps the hook (two timed faults); `--reset-source esp` there, or without
    `--link esp`, is a `bad_argument`.
- Witness check (esp link): from the first `UPD` of a trial to its end, the status and banner lines
  (compared from the regex match on, so glued fragments compare equal) of the bridge stream and of
  the VCP must be the same list, else the note gets
  `witness_mismatch: link <n> vs vcp <m> status/banner lines; #<i>: '<a>' vs '<b>'`. It never
  changes the outcome. serial.log directions: `>` host → target, `<` target (bridge stream),
  `V` the VCP witness, `E` ESP lines, `#` host notes.
- Config: `stress.toml` `[esp] port` (the CH343 `usb-1a86_USB_Single_Serial_*` port), `baud = 921600`.
  `--config` (global or after the subcommand) picks the bench config; for `--target c031` the default
  is `[c031] bench_config` = `bench-c031.toml`, and the C031 never falls back to bench.toml.
  `doctor --target c031` checks the esp port and PINGs the bridge (skipped with `--no-hardware`).
- Simulator: `stress/sim_esp.py` is the bridge in front of the sim board (same PC-side protocol,
  ESP latency, NRST timing with a small random lateness), and the sim board copies every line to a
  second UART for the witness, so `--sim --link esp` and `--sim --reset-source esp` run end to end.
- Bench config: `bench-c031.toml` (`--config`), OpenOCD from git (`~/.local/openocd-git/bin/openocd`,
  `target/stm32c0x.cfg`), serial port = the ST-LINK VCP.

## 4. Flashing the C031
Through bench's Programmer (OpenOCD with `stm32c0x.cfg`). The `NOD_C031C6` mass-storage drive is
only a fallback for a human.
