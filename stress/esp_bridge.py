"""ESP32-S3 line bridge (SPEC-ESP32 2, 3): the host's transport to the C031 through esp32/bridge.

EspBridgeTransport has bench's transport interface (write(bytes), read_available(timeout) -> str,
close()), so Link, Updater and the runner use it unchanged:
- write(): every complete printable line goes out as `TX <line>` (the ESP adds CR LF); anything
  else (a cut line, control bytes) as `TXH <hex>`. write_raw() (injected/corrupted bytes) always
  uses TXH.
- read_available(): `RX <text>` lines are unwrapped (`\\xHH` decoded) and returned as the target's
  line stream; `ESP ...` lines go to a separate queue (`esp_lines`) and to serial.log as `E`.
- control: ping, nrst_pulse, arm_tx, arm_rx, disarm, stat, time_on/off. `ESP NRST FIRED` lines are
  collected in `fires` (planned delay and late_us), the reset the host explains with them.
- timing (`TIME ON`, a tezgah extension, SPEC-ESP32 2.1): `ESP TXDONE <n> <t_us>` after each TX and
  `ESP RXT <t_us>` before each RX line give ESP-clock timestamps (`timeline`), which `calibrate`
  turns into per-line reset windows for `--reset-source esp`.

Witness: the C031 prints every line on both UARTs. A Witness reads the other one (the ST-LINK VCP),
logs it as `V`, and `compare()` checks that it saw the same status/banner lines.

The raw port is anything with write/read_available/close: bench.SerialTransport on hardware
(921600 8N1, the CH343 UART0 port), stress.sim_esp.SimEspPort in --sim, a fake in the unit tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._bench import BenchError
from .proto import parse_status, parse_banner

BRIDGE_ID = "tezgah-bridge"
BRIDGE_VERSION = "0.1"
ESP_BAUD = 921600
TX_MAX = 1000                 # longest `TX` text the bridge accepts (its PC line buffer is 2100)
TXH_MAX = 512                 # bytes per TXH command
PONG_RE = re.compile(r"^ESP PONG (\S+) (\S+)$")
READY_RE = re.compile(r"^ESP READY (\S+) (\S+)$")
FIRED_RE = re.compile(r"^ESP NRST FIRED (\d+) (-?\d+)$")
JITTER_RE = re.compile(r"^ESP ARMJITTER (-?\d+)$")
STAT_RE = re.compile(r"^ESP STAT (.*)$")
TXDONE_RE = re.compile(r"^ESP TXDONE (\d+) (\d+)$")
RXT_RE = re.compile(r"^ESP RXT (\d+)$")
HEXESC_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")


def escape(data: bytes) -> str:
    """How the bridge shows a received line: bytes < 0x20, > 0x7E and the backslash as \\xHH."""
    return "".join(chr(b) if 0x20 <= b <= 0x7E and b != 0x5C else f"\\x{b:02X}" for b in data)


def unescape(text: str) -> str:
    return HEXESC_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def printable(line: str) -> bool:
    return all(0x20 <= ord(c) <= 0x7E for c in line)


@dataclass
class Fire:
    delay_us: int
    late_us: int
    t: float                  # host time the line arrived
    jitter_us: int | None = None


@dataclass
class TimedTx:
    kind: str                 # "tx"
    text: str
    t_us: int | None = None   # ESP time the last byte left (from ESP TXDONE)


@dataclass
class TimedRx:
    text: str
    t_us: int                 # ESP time the line was complete


class EspBridgeTransport:
    def __init__(self, raw, clock=None, ping: bool = True, ping_timeout_s: float = 3.0):
        self.raw = raw
        self.clock = clock
        self.buf = ""
        self._out: list[str] = []
        self.esp_lines: list[tuple[float, str]] = []
        self.fires: list[Fire] = []
        self.ready_seen = 0
        self.unknown: list[str] = []
        self.timing = False
        self.timeline: list = []          # TimedTx / TimedRx in order (TIME ON only)
        self._tx_wait: list[TimedTx] = []
        self._rxt: int | None = None
        self._log = None
        self.version: str | None = None
        if ping:
            self.version = self.ping(timeout=ping_timeout_s)

    # --- plumbing --------------------------------------------------------------------
    def attach(self, link):
        """Link calls this: ESP lines are logged to serial.log with direction E."""
        self._log = link._log
        self.clock = self.clock or link.clock
        for _, ln in self.esp_lines:                 # what came before the link existed (READY, PONG)
            if not (TXDONE_RE.match(ln) or RXT_RE.match(ln)):
                self._log("E", ln)

    def _now(self) -> float:
        return self.clock.now() if self.clock is not None else 0.0

    def _cmd(self, text: str):
        self.raw.write((text + "\n").encode("ascii"))

    def _feed(self, text: str):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            if line.startswith("RX ") or line == "RX":
                body = unescape(line[3:])
                self._out.append(body + "\n")
                if self.timing and self._rxt is not None:
                    self.timeline.append(TimedRx(body, self._rxt))
                self._rxt = None
            elif line.startswith("ESP "):
                self._esp(line)
            else:                               # ESP ROM boot log, line noise on a reset
                self.unknown.append(line)
                if self._log:
                    self._log("E", f"? {line[:120]!r}")

    def _esp(self, line: str):
        self.esp_lines.append((self._now(), line))
        if m := TXDONE_RE.match(line):
            if self._tx_wait:
                tx = self._tx_wait.pop(0)
                tx.t_us = int(m.group(2))
            return                              # not logged: one per TX would double serial.log
        if m := RXT_RE.match(line):
            self._rxt = int(m.group(1))
            return
        if self._log:
            self._log("E", line)
        if m := FIRED_RE.match(line):
            self.fires.append(Fire(int(m.group(1)), int(m.group(2)), self._now()))
        elif m := JITTER_RE.match(line):
            if self.fires and self.fires[-1].jitter_us is None:
                self.fires[-1].jitter_us = int(m.group(1))
        elif READY_RE.match(line):
            self.ready_seen += 1

    def _poll(self, timeout: float):
        data = self.raw.read_available(max(0.0, timeout))
        if data:
            self._feed(data)

    def _wait_esp(self, pred, timeout: float, start: int | None = None) -> str | None:
        """Wait for an ESP line (from index `start` of esp_lines) with pred(line) true."""
        i = len(self.esp_lines) if start is None else start
        deadline = self._now() + timeout
        real = self.clock is None
        if real:
            import time
            deadline = time.monotonic() + timeout
        while True:
            while i < len(self.esp_lines):
                ln = self.esp_lines[i][1]
                i += 1
                if pred(ln):
                    return ln
            if real:
                import time
                left = deadline - time.monotonic()
            else:
                left = deadline - self._now()
            if left <= 1e-6:
                return None
            self._poll(min(0.05, left))

    def _tx_record(self, text: str):
        if self.timing:
            tx = TimedTx("tx", text)
            self.timeline.append(tx)
            self._tx_wait.append(tx)

    # --- transport interface -------------------------------------------------------------
    def write(self, data: bytes):
        text = data.decode("latin-1")
        parts = text.split("\n")
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                if part:
                    self.write_raw(part.encode("latin-1"))
                break
            line = part[:-1] if part.endswith("\r") else part
            if printable(line) and len(line) <= TX_MAX:
                self._tx_record(line)
                self._cmd(f"TX {line}")
            else:
                self.write_raw((line + "\r\n").encode("latin-1"))

    def write_raw(self, data: bytes):
        for o in range(0, len(data), TXH_MAX):
            piece = data[o:o + TXH_MAX]
            self._tx_record(piece.decode("latin-1"))
            self._cmd(f"TXH {piece.hex().upper()}")

    def read_available(self, timeout: float) -> str:
        if not self._out:
            self._poll(timeout)
        out, self._out = "".join(self._out), []
        return out

    def close(self):
        try:
            self.disarm(timeout=0.3)
        except Exception:
            pass
        self.raw.close()

    # --- control ----------------------------------------------------------------------
    def ping(self, timeout: float = 1.0) -> str:
        """`PING` -> `ESP PONG tezgah-bridge 0.1`. Retries for `timeout` (the ESP may be booting:
        opening its CH343 port can reset it). Returns the bridge version."""
        tries = max(1, int(timeout / 0.5))
        for _ in range(tries):
            start = len(self.esp_lines)
            self._cmd("PING")
            ln = self._wait_esp(lambda s: PONG_RE.match(s) is not None, 0.5, start)
            if ln:
                m = PONG_RE.match(ln)
                if m.group(1) != BRIDGE_ID:
                    raise BenchError("esp_wrong_firmware", f"The ESP32 answers '{ln}', not {BRIDGE_ID}.",
                                     ["Upload esp32/bridge (see esp32/bridge/README.md)"])
                return m.group(2)
        raise BenchError("esp_no_reply", "The ESP32 bridge does not answer PING.",
                         ["Check [esp] port in stress.toml (the CH343 'USB Single Serial' port, 921600)",
                          "Upload esp32/bridge (esp32/bridge/README.md)",
                          "Opening the port may have left the ESP32 in its ROM download mode: press RST"])

    def _expect(self, cmd: str, ok_prefix: str, timeout: float = 1.0) -> str:
        start = len(self.esp_lines)
        self._cmd(cmd)
        ln = self._wait_esp(lambda s: s.startswith(ok_prefix) or s.startswith("ESP ERR"), timeout, start)
        if ln is None or ln.startswith("ESP ERR"):
            raise BenchError("esp_command_failed", f"ESP32 bridge: '{cmd}' -> {ln or 'no reply'}.",
                             ["Bridge firmware too old? Re-upload esp32/bridge"])
        return ln

    def nrst_pulse(self, us: int = 1000, timeout: float = 2.0) -> str:
        if not 10 <= us <= 1_000_000:
            raise BenchError("bad_argument", "NRST pulse must be 10..1000000 us.")
        return self._expect(f"NRST PULSE {int(us)}", "ESP OK PULSE", timeout + us / 1e6)

    def arm_tx(self, delay_us: int, n: int = 1) -> str:
        return self._expect(f"NRST ARM TX {int(delay_us)} {int(n)}", "ESP OK ARM")

    def arm_rx(self, delay_us: int, prefix: str) -> str:
        return self._expect(f"NRST ARM RX {int(delay_us)} {prefix}", "ESP OK ARM")

    def disarm(self, timeout: float = 1.0) -> str:
        return self._expect("NRST DISARM", "ESP OK DISARM", timeout)

    def stat(self) -> dict:
        ln = self._expect("STAT", "ESP STAT")
        return {k: int(v) for k, v in re.findall(r"(\w+)=(-?\d+)", ln)}

    def time_on(self, on: bool = True) -> None:
        self._expect(f"TIME {'ON' if on else 'OFF'}", "ESP OK TIME")
        self.timing = on
        if not on:
            self._tx_wait.clear()

    def fires_since(self, i: int) -> list[Fire]:
        return self.fires[i:]

    def settle_fires(self, i: int, until_s: float) -> list[Fire]:
        """Wait (up to until_s of host time) for an armed pulse that has not been reported yet."""
        deadline = self._now() + until_s
        while len(self.fires) <= i and self._now() < deadline:
            self._poll(min(0.05, max(0.0, deadline - self._now())))
        return self.fires[i:]


# --- witness (the VCP, USART2) -----------------------------------------------------------
@dataclass
class Witness:
    transport: object
    lines: list = field(default_factory=list)       # (host t, line)
    buf: str = ""
    _log: object = None
    clock: object = None

    def attach(self, link):
        self._log = link._log
        self.clock = link.clock
        link.side.append(self)

    def poll(self, link=None, timeout: float = 0.0):
        data = self.transport.read_available(timeout)
        if not data:
            return
        self.buf += data
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            t = self.clock.now() if self.clock else 0.0
            self.lines.append((t, line))
            if self._log:
                self._log("V", line)

    def mark(self) -> int:
        return len(self.lines)

    def settle(self, seconds: float = 0.2):
        if self.clock is None:
            self.poll(timeout=seconds)
            return
        end = self.clock.now() + seconds
        while self.clock.now() < end:
            self.poll()
            self.clock.sleep(min(0.05, end - self.clock.now()))
        self.poll()

    def close(self):
        self.transport.close()


def key_lines(lines: list[str]) -> list[str]:
    """Status and banner lines (from the regex match on, so glued fragments compare equal)."""
    out = []
    for ln in lines:
        if (st := parse_status(ln)) is not None:
            out.append(st.line)
        elif (b := parse_banner(ln)) is not None:
            out.append(b.line)
    return out


def compare(link_lines: list[str], witness_lines: list[str]) -> str | None:
    """None when the witness saw the same status/banner lines as the link, else a short note."""
    a, b = key_lines(link_lines), key_lines(witness_lines)
    if a == b:
        return None
    n = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    first = a[n][:40] if n < len(a) else "-"
    other = b[n][:40] if n < len(b) else "-"
    return f"witness_mismatch: link {len(a)} vs vcp {len(b)} status/banner lines; #{n}: '{first}' vs '{other}'"


def trigger_line(entry: tuple[str, int, int] | None) -> tuple[str, int] | None:
    """SPEC-ESP32 3: which protocol line arms the ESP pulse for a (PHASE, index, delay) entry.
    ERASE -> UPD BEGIN, DATA k -> UPD DATA chunk k, HDR/META -> UPD END."""
    if entry is None:
        return None
    phase, idx, _ = entry
    if phase == "ERASE":
        return ("BEGIN", 0)
    if phase == "DATA":
        return ("DATA", max(0, idx))
    if phase in ("HDR", "META"):
        return ("END", 0)
    return None


class EspArmer:
    """Updater.before_send hook: arms `NRST ARM TX <delay> 1` right before the trigger line, once."""

    def __init__(self, bridge: EspBridgeTransport, entry: tuple[str, int, int], link=None):
        self.bridge = bridge
        self.entry = entry
        self.trigger = trigger_line(entry)
        self.armed = False
        self.link = link
        self.fires_mark = len(bridge.fires)
        if self.trigger is None:
            raise BenchError("bad_argument", f"--reset-source esp cannot trigger on {entry[0]} "
                             "(only ERASE/DATA/HDR/META: the BEGIN, DATA k and END lines).")

    def __call__(self, kind: str, index: int):
        if self.armed or (kind, index) != self.trigger:
            return
        self.armed = True
        delay = int(self.entry[2])
        if self.link is not None:
            self.link.note(f"esp: NRST ARM TX {delay} 1 (on {kind} {index})")
        self.bridge.arm_tx(delay, 1)
