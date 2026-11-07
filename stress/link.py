"""Line session over a transport with a pluggable clock.

bench.Session measures time with time.monotonic(), which would make --sim runs wait in real time.
Link does the same job (pump, line split, transcript) but asks a clock for "now", so the identical
host code runs against bench.SerialTransport on hardware and against the simulator in --sim.
Transport interface (same as bench's): write(bytes), read_available(timeout_s) -> str, close().
"""
from __future__ import annotations

import time
from typing import IO

from .proto import Event, parse_line


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


class Link:
    def __init__(self, transport, clock, log: IO[str] | None = None):
        self.t = transport
        self.clock = clock
        self.log = log
        self.t0 = clock.now()
        self.buf = ""
        self.events: list[Event] = []     # every received line, parsed, in order
        self.cursor = 0                   # next unread event for next_event()
        self.unread_from = 0              # cursor before the last drain()
        self.side: list = []              # side channels (the witness VCP): poll(link) on every pump
        if hasattr(transport, "attach"):
            transport.attach(self)

    # --- transcript -------------------------------------------------------------
    def _log(self, d: str, text: str):
        if self.log is not None:
            self.log.write(f"{self.clock.now() - self.t0:10.4f} {d} {text}\n")

    def note(self, text: str):
        self._log("#", text)

    def flush_log(self):
        if self.log is not None:
            self.log.flush()

    # --- io -------------------------------------------------------------------------
    def pump(self, timeout: float) -> int:
        data = self.t.read_available(max(0.0, timeout))
        for ch in self.side:
            ch.poll(self)
        if not data:
            return 0
        self.buf += data
        n = 0
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            self.events.append(parse_line(line, self.clock.now()))
            self._log("<", line)
            n += 1
        return n

    def send_line(self, text: str, raw: bool = False):
        """raw: the line carries injected (corrupted) bytes; a bridge sends it as raw bytes."""
        self._log(">", text)
        self._write((text + "\r\n").encode("latin-1", errors="replace"), raw)

    def send_raw(self, data: bytes, note: str = ""):
        self._log(">", f"{data.decode('latin-1')!r} (raw{', ' + note if note else ''})")
        self._write(data, True)

    def _write(self, data: bytes, raw: bool):
        w = getattr(self.t, "write_raw", None) if raw else None
        (w or self.t.write)(data)

    def mark(self) -> int:
        return len(self.events)

    def drain(self, wait_s: float = 0.0):
        """Read whatever has arrived (waiting up to wait_s) and mark it as consumed.
        `unread_from` keeps where the consumed-but-unread lines start, for callers that must
        inspect what they skip (Updater.left_update_mode)."""
        deadline = self.clock.now() + wait_s
        self.unread_from = self.cursor
        self.pump(0)
        while deadline - self.clock.now() > 1e-6:
            self.pump(min(0.05, deadline - self.clock.now()))
        self.cursor = len(self.events)

    def next_event(self, timeout: float) -> Event | None:
        """Next unread event, waiting up to timeout seconds."""
        deadline = self.clock.now() + timeout
        while self.cursor >= len(self.events):
            remaining = deadline - self.clock.now()
            if remaining <= 1e-6:
                return None
            self.pump(min(0.25, remaining))
        e = self.events[self.cursor]
        self.cursor += 1
        return e

    def wait_for(self, pred, timeout: float) -> tuple[Event | None, list[Event]]:
        """Wait for an event with pred(e) true. Returns (event or None, events seen)."""
        deadline = self.clock.now() + timeout
        seen = []
        while True:
            e = self.next_event(max(0.0, deadline - self.clock.now()))
            if e is None:
                return None, seen
            seen.append(e)
            if pred(e):
                return e, seen

    def command(self, text: str, regex, timeout: float = 1.0) -> Event | None:
        """Send one line and wait for a reply line matching regex (compiled or str)."""
        import re
        rx = re.compile(regex) if isinstance(regex, str) else regex
        self.drain()
        self.send_line(text)
        e, _ = self.wait_for(lambda ev: rx.search(ev.line) is not None, timeout)
        return e

    def close(self):
        self.t.close()
        for ch in self.side:
            ch.close()
