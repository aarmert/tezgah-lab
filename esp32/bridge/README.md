# tezgah-bridge (ESP32-S3)

Line bridge with a precisely timed NRST between the PC and the NUCLEO-C031C6, for the bootloader
stress test (`public/stress/SPEC-ESP32.md`). The update logic stays in the Python host
(`stress/esp_bridge.py`); this firmware moves lines and fires the reset.

```
PC ──USB── ESP32-S3 UART0 (CH343, /dev/serial/by-id/usb-1a86_USB_Single_Serial_*)  921600 8N1
ESP32 GPIO17 (UART1 TX) ──► C031 PA10 (USART1 RX)          115200 8N1
ESP32 GPIO18 (UART1 RX) ◄── C031 PA9  (USART1 TX)          (internal pull-up on GPIO18)
ESP32 GPIO4  (open-drain, released) ──► C031 NRST
GND ── GND
```

Board: ESP32-S3 with 16 MB flash and 8 MB octal PSRAM (N16R8). Use the **UART** USB connector
(CH343), not the native USB one: USB CDC on boot is disabled, so `Serial` is UART0.

## Build (agents may do this)

```
python3 esp32/build.py --json
```

FQBN: `esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=default,PartitionScheme=app3M_fat9M_16MB`
(arduino-cli 1.5, esp32 core 3.3.x). Output: `esp32/build/bridge/bridge.ino.bin` (+ bootloader,
partitions, and a 16 MB `bridge.ino.merged.bin` for offset 0).

## Upload (humans only)

Agents must not upload. With the CH343 port, auto-reset into the ROM bootloader normally works:

```
PORT=$(ls /dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00)
arduino-cli upload --fqbn esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=default,PartitionScheme=app3M_fat9M_16MB \
    --input-dir esp32/build/bridge -p "$PORT" esp32/bridge
```

If it does not enter the bootloader: hold BOOT, press RST, release BOOT, upload again, then press RST.
Check: open the port at 921600 (`python3 -m serial.tools.miniterm "$PORT" 921600`), press RST: you see
the ROM boot log (at 115200, garbled), then `ESP READY tezgah-bridge 0.1`. Type `PING` →
`ESP PONG tezgah-bridge 0.1`, `SELFTEST` → `ESP SELFTEST n=20 late_us min=.. avg=.. max=..`.
Then: `python -m stress --json doctor --target c031` (it PINGs the bridge).

## Protocol

PC → ESP, one command per `\n`-terminated line:

| Command | Effect | Reply |
|---|---|---|
| `PING` | | `ESP PONG tezgah-bridge 0.1` |
| `TX <text>` | `<text>\r\n` to the STM32 | none |
| `TXH <hex>` | raw bytes to the STM32 (no CR/LF added) | none (`ESP ERR TXH` on bad hex) |
| `NRST PULSE <us>` | NRST low for `<us>` (10..1000000), then released | `ESP OK PULSE` (after the release) |
| `NRST ARM TX <delay_us> [<n>]` | 1 ms pulse `<delay_us>` after the last byte of the n-th following TX/TXH has left UART1 | `ESP OK ARM` |
| `NRST ARM RX <delay_us> <prefix>` | 1 ms pulse `<delay_us>` after a received line starting with `<prefix>` is complete | `ESP OK ARM` |
| `NRST DISARM` | cancel (also a pending timer) | `ESP OK DISARM` |
| `STAT` | | `ESP STAT rx_lines=.. tx_lines=.. fired=.. overruns=..` |
| `TIME ON\|OFF` (extension) | timestamps: `ESP TXDONE <n> <t_us>` after each TX/TXH, `ESP RXT <t_us>` before each `RX` line | `ESP OK TIME` |
| `SELFTEST` (extension) | timer wake-up lateness, NRST untouched | `ESP SELFTEST n=20 late_us min=.. avg=.. max=..` |

ESP → PC: `RX <text>` (one STM32 line, CR stripped; bytes < 0x20, > 0x7E and `\` as `\xHH`),
`ESP READY tezgah-bridge 0.1` after boot, `ESP NRST FIRED <delay_us> <late_us>` and
`ESP ARMJITTER <late_us>` after every armed pulse, `ESP OK ...` / `ESP ERR ...`.
A PC line longer than 2100 chars → `ESP ERR LINE`. An STM32 line longer than 1024 bytes is passed on
in 1024-byte `RX` pieces. Only one arm at a time; a new ARM replaces the old one. `NRST PULSE`
leaves a pending ARM in place.

## Timing

- `Serial1` has no TX ring buffer, so `write()` returns once every byte is in the UART FIFO, and
  `flush()` spins until the transmitter is idle: that is the end of the last stop bit, within a few
  µs. The pulse is planned at that moment + `<delay_us>`.
- Delays ≤ 300 µs are busy-waited in `loop()`. Longer ones use a one-shot `esp_timer` that wakes
  300 µs early and busy-waits the rest. The falling edge is read back from the pad (the pin is in
  input+open-drain mode), and `late_us` = edge − plan is printed with every fire.
- ARM RX: `Serial1.setRxFIFOFull(1)` moves every received byte to the ring buffer at once, and the
  `\n` is timestamped when `loop()` reads it (tens of µs after its stop bit, not corrected).
- **Measured on hardware (2026-11-07):** `late_us` of the armed NRST pulses was 2-15 µs (median 7 µs,
  over 3000+ pulses); `SELFTEST` reported 0-1 µs timer wake-up lateness. Still not measured:
  the NRST rise time. The Nucleo has a capacitor on NRST and the STM32 pull-up is ~40 kΩ, so after the
  release NRST takes a few ms to rise. The edge that matters for the test is the falling one.
- Timestamps from `TIME ON` use the ESP's `esp_timer` clock (µs since ESP boot). `stress calibrate
  --link esp` turns them into the per-line delay windows for `--reset-source esp`.
