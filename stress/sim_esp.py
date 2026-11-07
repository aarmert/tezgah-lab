"""Simulated ESP32-S3 bridge (esp32/bridge/bridge.ino) in front of a SimBoard, for --sim --link esp.

SimEspPort is the raw port the host's EspBridgeTransport talks to (what the CH343 serial port is on
hardware). It speaks the bridge's PC-side protocol line for line (SPEC-ESP32 2): TX/TXH go into the
board's UART RX after the ESP's own latency, every complete board line comes back as `RX <text>`
(escaped like the firmware), NRST PULSE / ARM TX / ARM RX / DISARM drive the board's external reset
(board.nrst_at), and STAT / PING / TIME behave like the firmware. Time is the board's simulated time.
"""
from __future__ import annotations

import math

from .esp_bridge import BRIDGE_ID, BRIDGE_VERSION, escape
from .sim import US_PER_CHAR, SimBoard

ESP_US_PER_CHAR = 11          # 921600 8N1 on the PC side


class SimEspPort:
    def __init__(self, board: SimBoard, banner: bool = True):
        self.b = board
        self.out: list[list] = []         # [t_available_us, text], in output order
        self.out_free = 0                 # the ESP's UART0 TX is busy until then
        self.inbuf = ""
        self.rxbuf = ""                   # bytes from the target's UART, not yet a whole line
        self.stats = {"rx_lines": 0, "tx_lines": 0, "fired": 0, "overruns": 0}
        self.arm = None                   # ("tx", delay, n) | ("rx", delay, prefix)
        self.timing = False
        self.planned = None               # (delay_us, planned edge t_us)
        self.closed = False
        board.on_nrst = self._fired
        if banner:
            self._emit(f"ESP READY {BRIDGE_ID} {BRIDGE_VERSION}")

    # --- helpers -----------------------------------------------------------------------
    @property
    def lat(self) -> int:
        return self.b.T.usb_latency_us

    def _emit(self, text: str, t: int | None = None):
        """One line out of the ESP's UART0, in order (921600 8N1), then the USB latency."""
        t = self.b.now if t is None else t
        start = max(t, self.out_free)
        self.out_free = start + (len(text) + 2) * ESP_US_PER_CHAR
        self.out.append([self.out_free + self.lat, text + "\r\n"])

    def _fired(self, t: int):
        """board.on_nrst: the armed edge went out (the board resets right after this)."""
        if self.planned is None:
            return
        delay, plan = self.planned
        self.planned = None
        late = t - plan
        self.stats["fired"] += 1
        self._emit(f"ESP NRST FIRED {delay} {late}", t + 1000)
        self._emit(f"ESP ARMJITTER {late}", t + 1000)

    def _schedule(self, delay: int, t_ref: int, hold_us: int = 1000):
        late = self.b.rng.randint(1, max(1, self.b.T.esp_late_us))
        plan = t_ref + delay
        self.planned = (delay, plan)
        self.b.nrst_at = plan + late
        self.b.nrst_hold_us = hold_us

    def _to_target(self, data: bytes) -> int:
        t_end = self.b.host_write(data, latency_us=self.lat + self.b.T.esp_us)
        self.stats["tx_lines"] += 1
        if self.timing:
            self._emit(f"ESP TXDONE {self.stats['tx_lines']} {t_end}", t_end)
        if self.arm and self.arm[0] == "tx":
            _, delay, n = self.arm
            if n <= 1:
                self.arm = None
                self._schedule(delay, t_end)
            else:
                self.arm = ("tx", delay, n - 1)
        return t_end

    def _command(self, line: str):
        if line == "PING":
            self._emit(f"ESP PONG {BRIDGE_ID} {BRIDGE_VERSION}")
        elif line == "TX" or line.startswith("TX "):
            self._to_target((line[3:] + "\r\n").encode("latin-1"))
        elif line.startswith("TXH "):
            h = line[4:].strip()
            try:
                data = bytes.fromhex(h)
            except ValueError:
                self._emit("ESP ERR TXH")
                return
            if not data:
                self._emit("ESP ERR TXH")
                return
            self._to_target(data)
        elif line.startswith("NRST PULSE "):
            try:
                us = int(line[11:])
            except ValueError:
                us = -1
            if not 10 <= us <= 1_000_000:
                self._emit("ESP ERR PULSE")
                return
            self.planned = None                   # (a pending ARM TX/RX stays armed, as in bridge.ino)
            self.b.nrst_at = self.b.now + self.lat
            self.b.nrst_hold_us = us
            self._emit("ESP OK PULSE", self.b.now + self.lat + us)
        elif line.startswith("NRST ARM TX "):
            p = line[12:].split()
            if len(p) not in (1, 2) or not all(x.isdigit() for x in p) or (len(p) == 2 and int(p[1]) < 1):
                self._emit("ESP ERR ARM")
                return
            self._disarm()
            self.arm = ("tx", int(p[0]), int(p[1]) if len(p) == 2 else 1)
            self._emit("ESP OK ARM")
        elif line.startswith("NRST ARM RX "):
            p = line[12:].split(" ", 1)
            if len(p) != 2 or not p[0].isdigit() or not p[1]:
                self._emit("ESP ERR ARM")
                return
            self._disarm()
            self.arm = ("rx", int(p[0]), p[1])
            self._emit("ESP OK ARM")
        elif line == "NRST DISARM":
            self._disarm()
            self._emit("ESP OK DISARM")
        elif line == "STAT":
            s = self.stats
            self._emit(f"ESP STAT rx_lines={s['rx_lines']} tx_lines={s['tx_lines']} fired={s['fired']} "
                       f"overruns={s['overruns']}")
        elif line in ("TIME ON", "TIME OFF"):
            self.timing = line == "TIME ON"
            self._emit("ESP OK TIME")
        elif line == "SELFTEST":
            self._emit("ESP SELFTEST n=20 late_us min=0 avg=1 max=3")
        elif line:
            self._emit("ESP ERR unknown command")

    def _disarm(self):
        self.arm = None
        if self.planned is not None:
            self.planned = None
            self.b.nrst_at = None

    def _collect(self):
        """Board UART output that has left the target by now -> RX lines (the ESP's line framing)."""
        b = self.b
        while b.tx and b.tx[0][1] <= b.now:
            t_end, text = b.tx[0][1], b.tx.pop(0)[2]
            self.rxbuf += text
            while "\n" in self.rxbuf:
                line, self.rxbuf = self.rxbuf.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    continue
                self.stats["rx_lines"] += 1
                t = t_end + b.T.esp_us
                if self.timing:
                    self._emit(f"ESP RXT {t_end}", t)
                self._emit("RX " + escape(line.encode("latin-1")), t)
                if self.arm and self.arm[0] == "rx" and line.startswith(self.arm[2]):
                    _, delay, _ = self.arm
                    self.arm = None
                    self._schedule(delay, t_end)

    # --- raw port interface ------------------------------------------------------------------
    def write(self, data: bytes):
        self.inbuf += data.decode("latin-1")
        while "\n" in self.inbuf:
            line, self.inbuf = self.inbuf.split("\n", 1)
            self._command(line.rstrip("\r"))

    def read_available(self, timeout: float) -> str:
        b = self.b
        deadline = b.now + math.ceil(max(0.0, timeout) * 1e6)
        while True:
            self._collect()
            ready = [e for e in self.out if e[0] <= b.now]
            if ready:
                self.out = [e for e in self.out if e[0] > b.now]
                return "".join(e[1] for e in ready)
            if b.now >= deadline:
                return ""
            nxt = min([e[0] for e in self.out] + [deadline])
            b.advance(nxt, stop_on_output=True)

    def close(self):
        self.closed = True


assert US_PER_CHAR > ESP_US_PER_CHAR
