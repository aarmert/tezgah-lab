"""Simulated STM32F407 bench: both bootloader designs, the apps, F407 flash and the fault hook.

Discrete-event model in simulated microseconds. Device firmware is written as generators that
yield requests to the scheduler:
    ("sleep", us)                          CPU busy, UART RX still captured by the ISR
    ("flash", us, done, partial)           flash erase/program; the UART is not read (bytes lost);
                                           a reset in the middle calls partial(fraction)
    ("read", timeout_us | None)            wait for a received line (None on timeout)
    ("reset", cause)                       NVIC_SystemReset / SYSRESETREQ
    ("halt",)                              lockup or endless loop; only a reset ends it
Flash semantics: erased = 0xFF, programming only clears bits (new = old & data), a reset during an
erase leaves a random mix of erased and old bits, a reset during programming leaves the words
before the cut programmed and the word under the cut partially programmed.

The sim plugs into the host as a transport (SimSerial), a clock (SimClock) and a bench Programmer
(SimProgrammer) that understands `dump_image` and `mdw`, so `--sim` runs the identical host code.
"""
from __future__ import annotations

import base64
import hashlib
import math
import re
import struct
import random
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ._bench import bench
from .images import header_error, parse_header, slot_status_word, slot_valid
from .layout import (CHUNK, GOLDEN_SIZE, HEADER_SIZE, MAX_LINE, PHASES, SECTORS, SLOT_BASE, SLOT_NUM,
                     SLOT_NAME, SLOT_SECTOR, TRIAL_NONE, UID_ADDR, looks_like_sp, other_meta, other_slot)
from .meta import free_index, latest, pack_record
from .simimages import BL_MARK, REGIONS, app_registry
from .stlink import ihex_to_segments

US_PER_CHAR = 87           # 115200 8N1
SIM_UID = "0033002A3438511234363632"


@dataclass
class Timings:
    erase16_us: int = 260_000      # F407 16 KB sector, x32: typ 250 ms
    erase64_us: int = 560_000      # 64 KB sector: typ 550 ms
    erase_jitter: float = 0.08
    word_us: int = 16              # word program, x32: typ 16 us
    crc_us_per_kb: int = 400
    sha_us_per_kb: int = 3000
    reset_us: int = 300
    bl_init_us: int = 1200
    app_init_us: int = 2500
    iwdg_s: float = 2.0            # LSI/32, reload 2000
    lsi_jitter: float = 0.10
    confirm_after_s: float = 1.0
    trial_deadline_s: float = 5.0
    update_timeout_s: float = 3.0
    usb_latency_us: int = 2000
    rx_dead_us: int = 1000         # UART not initialised yet after a reset
    stlink_read_s: float = 0.8
    stlink_flash_s: float = 4.0
    stlink_reset_s: float = 0.3


@dataclass
class NoInit:
    enter_update: bool = False
    update_in_progress: bool = False
    fault: list = field(default_factory=lambda: [None, None])   # (phase, idx, delay_us)
    fired_count: int = 0
    last_fired: tuple | None = None
    trace: bool = False


class SimBoard:
    def __init__(self, seed: int = 0, hooks: bool = True, timings: Timings | None = None,
                 golden: bytes | None = None):
        self.T = timings or Timings()
        self.rng = random.Random(f"sim:{seed}")
        self.hooks = hooks
        self.mem = bytearray(b"\xff" * GOLDEN_SIZE)
        self.now = 0
        self.apps = app_registry()
        self.ni = NoInit()
        self.rx: deque = deque()            # [start, end, text, complete]
        self.rx_free = 0
        self.rx_partial = ""
        self.tx: list = []                  # [t_start, t_end, text]
        self.tx_free = 0
        self.busy: list[tuple[int, int]] = []
        self.proc = None
        self.req = None
        self.req_t = 0
        self.tim2_at = None
        self.tim2_entry = None
        self.iwdg_running = False
        self.iwdg_at = None
        self.boot_t = 0
        self.app_t = 0
        self.reset_cause = "POR"
        self.design = "?"
        self.resets = 0
        if golden is not None:
            self.mem[:len(golden)] = golden
        self.reset("POR")

    # --- flash ---------------------------------------------------------------------
    def rd(self, addr: int, n: int) -> bytes:
        o = addr - SECTORS[0][0]
        if 0 <= o and o + n <= len(self.mem):
            return bytes(self.mem[o:o + n])
        return b"\xff" * n

    def sector(self, s: int) -> bytes:
        base, size = SECTORS[s]
        return self.rd(base, size)

    def slot(self, s: str) -> bytes:
        return self.sector(SLOT_SECTOR[s])

    def _erase_op(self, s: int):
        base, size = SECTORS[s]
        o = base - SECTORS[0][0]
        nominal = self.T.erase16_us if size == 0x4000 else self.T.erase64_us
        dur = int(nominal * (1 + self.T.erase_jitter * (2 * self.rng.random() - 1)))

        def done():
            self.mem[o:o + size] = b"\xff" * size

        def partial(frac: float):
            if frac <= 0:
                return
            rnd = self.rng.randbytes(size)
            noise = self.rng.randbytes(size)
            cut = int(frac * 255)
            buf = self.mem[o:o + size]
            for i in range(size):
                r = rnd[i]
                if r < cut * 0.9:
                    buf[i] = 0xFF
                elif r < cut:
                    buf[i] |= noise[i]
            self.mem[o:o + size] = buf

        return ("flash", dur, done, partial)

    def _program_op(self, addr: int, data: bytes):
        o = addr - SECTORS[0][0]
        n = len(data) // 4
        dur = max(1, n * self.T.word_us)

        def write(k: int):
            for i in range(k * 4):
                self.mem[o + i] &= data[i]

        def done():
            write(n)

        def partial(frac: float):
            x = frac * n
            k = int(x)
            write(k)
            if k < n and x > k:
                for i in range(k * 4, k * 4 + 4):
                    self.mem[o + i] &= data[i] | self.rng.getrandbits(8)

        return ("flash", dur, done, partial)

    def load_segments(self, segs: list[tuple[int, bytes]]):
        """ST-Link programming: erase the touched sectors, then write (no partial effects)."""
        touched = set()
        for addr, data in segs:
            for s, (base, size) in SECTORS.items():
                if addr < base + size and addr + len(data) > base:
                    touched.add(s)
        for s in touched:
            base, size = SECTORS[s]
            o = base - SECTORS[0][0]
            self.mem[o:o + size] = b"\xff" * size
        for addr, data in segs:
            o = addr - SECTORS[0][0]
            if o < 0 or o + len(data) > len(self.mem):
                raise bench.BenchError("image_invalid", "[sim] image outside the simulated flash")
            self.mem[o:o + len(data)] = data

    # --- scheduler -----------------------------------------------------------------------
    def reset(self, cause: str):
        r = self.req
        if r is not None and r[0] == "flash":
            end = self.req_t + r[1]
            if self.now >= end:
                r[2]()
            else:
                r[3]((self.now - self.req_t) / r[1])
            self.busy.append((self.req_t, self.now))
        old, self.proc, self.req = self.proc, None, None
        if old is not None:
            old.close()
        kept = []
        for e in self.tx:
            if e[0] >= self.now:
                continue
            if e[1] > self.now:
                e[2] = e[2][:int((self.now - e[0]) / US_PER_CHAR)].rstrip("\r\n")
                e[1] = self.now
            kept.append(e)
        self.tx = kept
        self.tx_free = self.now
        self.rx.clear()
        self.rx_partial = ""
        self.tim2_at = self.tim2_entry = None
        self.iwdg_running, self.iwdg_at = False, None
        self.boot_t = self.now
        self.reset_cause = cause
        self.resets += 1
        self.proc = self._boot()
        self._resume(None)

    def _resume(self, value):
        try:
            req = self.proc.send(value)
        except StopIteration:
            req = ("halt",)
        self.req, self.req_t = req, self.now
        if req[0] == "reset":
            self.reset(req[1])

    def _next_rx_time(self):
        return self.rx[0][1] if self.rx else None

    def _wake_time(self):
        r = self.req
        if r is None or r[0] == "halt":
            return None
        if r[0] in ("sleep", "flash"):
            return self.req_t + r[1]
        if r[0] == "read":
            cands = []
            if self.rx:
                cands.append(max(self.rx[0][1], self.now))
            if r[1] is not None:
                cands.append(self.req_t + r[1])
            return min(cands) if cands else None
        return None

    def _dropped(self, start: int, end: int) -> bool:
        if start < self.boot_t + self.T.rx_dead_us:
            return True
        return any(start < b1 and end > b0 for b0, b1 in self.busy)

    def _take_line(self):
        while self.rx and self.rx[0][1] <= self.now:
            start, end, text, complete = self.rx.popleft()
            if self._dropped(start, end):
                continue
            self.rx_partial += text
            if complete:
                line, self.rx_partial = self.rx_partial, ""
                return line
        return None

    def _wake(self):
        r = self.req
        if r[0] == "sleep":
            self._resume(None)
        elif r[0] == "flash":
            r[2]()
            self.busy.append((self.req_t, self.now))
            if len(self.busy) > 64:
                self.busy = [b for b in self.busy if b[1] > self.now - 10_000_000]
            self._resume(None)
        elif r[0] == "read":
            line = self._take_line()
            if line is not None:
                self._resume(line)
            elif r[1] is not None and self.now >= self.req_t + r[1]:
                self._resume(None)

    def _next_output_time(self):
        return self.tx[0][1] + self.T.usb_latency_us if self.tx else None

    def advance(self, until: int, stop_on_output: bool = False):
        guard = 0
        while True:
            guard += 1
            if guard > 2_000_000:
                raise RuntimeError("sim scheduler is not making progress")
            cands = []
            if self.tim2_at is not None:
                cands.append((self.tim2_at, 0))
            if self.iwdg_at is not None:
                cands.append((self.iwdg_at, 1))
            w = self._wake_time()
            if w is not None:
                cands.append((w, 2))
            if stop_on_output:
                o = self._next_output_time()
                if o is not None:
                    cands.append((o, 3))
            if not cands:
                self.now = max(self.now, until)
                return
            t, kind = min(cands)
            if t > until:
                self.now = max(self.now, until)
                return
            self.now = max(self.now, t)
            if kind == 3:
                return
            if kind == 0:
                self.ni.fired_count += 1
                self.ni.last_fired = self.tim2_entry
                self.reset("PIN,SFT")
            elif kind == 1:
                self.reset("IWDG")
            else:
                self._wake()

    # --- host side -------------------------------------------------------------------
    def host_write(self, data: bytes):
        text = data.decode("latin-1").replace("\r", "")
        parts = text.split("\n")
        for i, p in enumerate(parts):
            complete = i < len(parts) - 1
            if not p and not complete:
                continue
            start = max(self.now + self.T.usb_latency_us, self.rx_free)
            end = start + (len(p) + (2 if complete else 0)) * US_PER_CHAR
            self.rx_free = end
            self.rx.append([start, end, p, complete])

    def host_read(self, timeout_s: float) -> str:
        self.advance(self.now + math.ceil(max(0.0, timeout_s) * 1e6), stop_on_output=True)
        out = []
        lat = self.T.usb_latency_us
        while self.tx and self.tx[0][1] + lat <= self.now:
            out.append(self.tx.pop(0)[2])
        return "".join(out)

    # --- device helpers ----------------------------------------------------------------
    def _print(self, text: str):
        start = max(self.now, self.tx_free)
        end = start + (len(text) + 2) * US_PER_CHAR
        self.tx_free = end
        self.tx.append([start, end, text + "\r\n"])

    def _flush(self):
        return ("sleep", max(0, self.tx_free - self.now))

    def _trace(self, phase: str, idx: int, us: int):
        if self.hooks and self.ni.trace:
            self._print(f"T {phase} {idx} {int(us)}")

    def _fault_point(self, phase: str, idx: int):
        if not self.hooks or self.tim2_at is not None:
            return
        for n in (0, 1):
            e = self.ni.fault[n]
            if e and e[0] == phase and (e[1] == -1 or e[1] == idx):
                self.ni.fault[n] = None                     # disarm in noinit first
                self.tim2_at = self.now + e[2]
                self.tim2_entry = (n, phase, idx, e[2])
                return

    def _iwdg_period(self) -> int:
        return int(self.T.iwdg_s * 1e6 * (1 + self.T.lsi_jitter * (2 * self.rng.random() - 1)))

    def _meta(self):
        return latest(self.sector(1), self.sector(4))

    def _valid(self, s: str):
        return slot_valid(self.slot(s), s)

    def _crc_cost(self, s: str) -> int:
        h = parse_header(self.slot(s))
        if header_error(h, s) is not None:
            return 50
        return self.T.crc_us_per_kb * h.payload_size // 1024

    # --- bootloader ------------------------------------------------------------------------
    def _boot(self):
        yield ("sleep", self.T.reset_us + self.T.bl_init_us)
        bl = self.rd(SECTORS[0][0], 32)
        design = next((d for d, m in BL_MARK.items() if bl.startswith(m)), None)
        if design is None:
            yield ("halt",)                                  # blank or unknown BL: silent
        self.design = design
        t0 = self.now
        self._fault_point("BOOT", 0)
        if design == "ab":
            yield from self._bl_ab(t0)
        else:
            yield from self._bl_naive(t0)
        yield ("halt",)

    def _status(self, design: str) -> str:
        a = slot_status_word(self.slot("A"), "A")
        if design == "ab":
            rec = self._meta()
            b = slot_status_word(self.slot("B"), "B")
            active = rec.active_slot if rec else "-"
            state = rec.state if rec else "none"
            att, seq = (rec.attempts, rec.seq) if rec else (0, 0)
            e0, e1 = rec.erases() if rec else (0, 0)
        else:
            b, state, att, seq, e0, e1 = "-", "none", 0, 0, 0, 0
            w0 = struct.unpack("<I", self.slot("A")[HEADER_SIZE:HEADER_SIZE + 4])[0]
            active = "A" if looks_like_sp(w0) else "-"
        return (f"BL v0.1 design={design}{' hooks' if self.hooks else ''} slotA={a} slotB={b} "
                f"active={active} state={state} attempts={att} seq={seq} erases={e0},{e1} "
                f"reset={self.reset_cause}")

    def _print_fired(self):
        if self.ni.fired_count and self.ni.last_fired:
            n, phase, idx, delay = self.ni.last_fired
            self._print(f"BL fault_fired {self.ni.fired_count} {phase} {idx} {delay}")
        self.ni.fired_count, self.ni.last_fired = 0, None

    def _bl_ab(self, t0: int):
        yield ("sleep", self._crc_cost("A") + self._crc_cost("B"))
        self._print(self._status("ab"))
        self._print_fired()
        if self.ni.enter_update or self.ni.update_in_progress:
            word = "update" if self.ni.enter_update else "resume"
            self.ni.enter_update = False
            self._trace("BOOT", 0, self.now - t0)
            self._print(f"BL {word}")
            yield from self._update_mode(timeout=True)
        else:
            self._trace("BOOT", 0, self.now - t0)
        yield from self._decide_ab()

    def _decide_ab(self):
        rec = self._meta()
        valid = {s: self._valid(s) for s in "AB"}
        if rec is None:
            for s in "AB":
                ok, h = valid[s]
                if ok:
                    yield from self._meta_append(1, SLOT_NUM[s], TRIAL_NONE, 0, h.security_counter)
                    self._print(f"BL jump {s}")
                    yield from self._jump(s, False)
            yield from self._nofw()
        if rec.trial != TRIAL_NONE:
            t = rec.trial_slot
            if rec.attempts >= 3 or not valid[t][0]:
                yield from self._meta_append(5, rec.active, TRIAL_NONE, 0, rec.min_security_counter)
                self._print(f"BL rollback {t}->{rec.active_slot}")
                rec = self._meta() or rec
            else:
                yield from self._meta_append(3, rec.active, rec.trial, rec.attempts + 1, rec.min_security_counter)
                self.iwdg_running = True
                self.iwdg_at = self.now + self._iwdg_period()
                self._print(f"BL jump {t} trial")
                yield from self._jump(t, True)
        a = rec.active_slot
        if valid[a][0]:
            self._print(f"BL jump {a}")
            yield from self._jump(a, False)
        o = other_slot(a)
        if valid[o][0]:
            yield from self._meta_append(6, SLOT_NUM[o], TRIAL_NONE, 0, rec.min_security_counter)
            self._print(f"BL repair active={o}")
            self._print(f"BL jump {o}")
            yield from self._jump(o, False)
        yield from self._nofw()

    def _bl_naive(self, t0: int):
        yield ("sleep", 200)
        self._print(self._status("naive"))
        self._print_fired()
        self._trace("BOOT", 0, self.now - t0)
        if self.ni.enter_update:
            self.ni.enter_update = False
            self._print("BL update")
            yield from self._update_mode(timeout=True)
        # Deliberately unsafe: no CRC, no header check, no resume. Word 0 of the payload decides.
        w0 = struct.unpack("<I", self.slot("A")[HEADER_SIZE:HEADER_SIZE + 4])[0]
        if looks_like_sp(w0):
            self._print("BL jump A")
            yield from self._jump("A", False)
        yield from self._nofw()

    def _nofw(self):
        self._print("BL nofw")
        yield from self._update_mode(timeout=False)
        yield ("halt",)

    def _jump(self, s: str, trial: bool):
        self._fault_point("JUMP", SLOT_NUM[s])
        t = self.now
        yield ("sleep", 20)
        self._trace("JUMP", SLOT_NUM[s], self.now - t)
        yield from self._app(s)
        yield ("halt",)

    def _meta_append(self, kind: int, active: int, trial: int, attempts: int, min_sc: int):
        rec = self._meta()
        cur = rec.sector if rec else 1
        seq = rec.seq + 1 if rec else 1
        e = dict(zip((1, 4), rec.erases())) if rec else {1: 0, 4: 0}
        idx = free_index(self.sector(cur))
        if idx is None:
            other = other_meta(cur)
            self._fault_point("ROTATE", other)
            op = self._erase_op(other)
            yield op
            self._trace("ROTATE", other, op[1])
            e[other] += 1
            cur, idx = other, 0
        r = pack_record(seq, kind, active, trial, attempts, min_sc, e[cur], e[other_meta(cur)])
        addr = SECTORS[cur][0] + idx * 32
        self._fault_point("META", kind)
        t = self.now
        yield self._program_op(addr, r[:28])
        yield self._program_op(addr + 28, r[28:])          # CRC word last
        self._trace("META", kind, self.now - t)

    # --- update mode ---------------------------------------------------------------------
    def _update_mode(self, timeout: bool):
        sess = None
        while True:
            line = yield ("read", int(self.T.update_timeout_s * 1e6) if timeout else None)
            if line is None:
                self._print("BL timeout")
                self.ni.update_in_progress = self.ni.enter_update = False
                return
            line = line.strip()
            if not line:
                continue
            if len(line) > MAX_LINE:
                self._print("ERR LINE")
            elif line.startswith("FAULT"):
                self._fault_cmd(line, bl=True)
            elif line == "UPD ABORT":
                self.ni.update_in_progress = False
                self._print("OK ABORT")
                yield self._flush()
                yield ("reset", "SFT")
            elif line.startswith("UPD BEGIN "):
                sess = yield from self._upd_begin(line[len("UPD BEGIN "):])
            elif line.startswith("UPD DATA "):
                if sess is None:
                    self._print("ERR STATE")
                else:
                    yield from self._upd_data(sess, line)
            elif line == "UPD END":
                if sess is None:
                    self._print("ERR STATE")
                else:
                    yield from self._upd_end(sess)
            else:
                self._print("ERR LINE")

    def _upd_begin(self, b64: str):
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            raw = b""
        if len(raw) != HEADER_SIZE:
            self._print("ERR BAD_HDR")
            return None
        h = parse_header(raw)
        ab = self.design == "ab"
        rec = self._meta() if ab else None
        if ab:
            target = other_slot(rec.active_slot) if rec else ("B" if self._valid("A")[0] else "A")
        else:
            target = "A"
        code = header_error(h, target)
        if code is None and ab and rec is not None and h.security_counter < rec.min_security_counter:
            code = "VERSION"
        if code:
            self._print(f"ERR {code}")
            return None
        if ab:
            self.ni.update_in_progress = True
        sector = SLOT_SECTOR[target]
        self._fault_point("ERASE", sector)
        op = self._erase_op(sector)
        yield op
        self._trace("ERASE", sector, op[1])
        self._print(f"OK ERASED {op[1]}")
        return {"h": h, "raw": raw, "target": target, "written": 0, "chunks": 0,
                "base": SLOT_BASE[target] + HEADER_SIZE}

    def _upd_data(self, s: dict, line: str):
        m = re.fullmatch(r"UPD DATA (\d+) (\S+) ([0-9a-fA-F]{8})", line)
        if not m:
            self._print("ERR LINE")
            return
        off = int(m.group(1))
        if off % 4 or off > s["written"]:
            self._print(f"ERR SEQ {s['written']}")
            return
        try:
            data = base64.b64decode(m.group(2), validate=True)
        except (ValueError, base64.binascii.Error):
            self._print(f"ERR CRC {off}")
            return
        if zlib.crc32(data) != int(m.group(3), 16):
            self._print(f"ERR CRC {off}")
            return
        if not data or len(data) > CHUNK or len(data) % 4:
            self._print("ERR LINE")
            return
        if off + len(data) > s["h"].payload_size:
            self._print("ERR SIZE")
            return
        if off < s["written"]:
            same = off + len(data) <= s["written"] and self.rd(s["base"] + off, len(data)) == data
            self._print(f"OK {off}" if same else f"ERR SEQ {s['written']}")
            return
        k = s["chunks"]
        self._fault_point("DATA", k)
        op = self._program_op(s["base"] + off, data)
        yield op
        self._trace("DATA", k, op[1])
        s["written"] += len(data)
        s["chunks"] += 1
        self._print(f"OK {off}")

    def _upd_end(self, s: dict):
        h, target = s["h"], s["target"]
        slot_base = SLOT_BASE[target]
        if self.design == "ab":
            if s["written"] < h.payload_size:
                self._print("ERR SHORT")
                return
            payload = self.rd(slot_base + HEADER_SIZE, h.payload_size)
            yield ("sleep", self.T.crc_us_per_kb * h.payload_size // 1024)
            if zlib.crc32(payload) != h.payload_crc32:
                self._print("ERR IMG_CRC")
                return
            yield ("sleep", self.T.sha_us_per_kb * h.payload_size // 1024)
            if hashlib.sha256(payload).digest() != h.payload_sha256:
                self._print("ERR IMG_SHA")
                return
        self._fault_point("HDR", 0)
        op = self._program_op(slot_base, s["raw"])
        yield op
        self._trace("HDR", 0, op[1])
        if self.design == "ab":
            rec = self._meta()
            active = rec.active if rec else SLOT_NUM[other_slot(target)]
            min_sc = rec.min_security_counter if rec else h.security_counter
            yield from self._meta_append(2, active, SLOT_NUM[target], 0, min_sc)
            self.ni.update_in_progress = False
        self._print("OK COMMIT")
        yield self._flush()
        yield ("reset", "SFT")

    def _fault_cmd(self, line: str, bl: bool):
        unknown = "ERR LINE" if bl else "ERR unknown command"
        if not self.hooks:
            self._print(unknown)
            return
        p = line.split()
        if len(p) in (6, 7) and p[1] == "ARM" and p[2] in ("0", "1") and p[3] in PHASES \
                and re.fullmatch(r"-?\d+", p[4]) and p[5].isdigit() and (len(p) == 6 or p[6] == "reset"):
            self.ni.fault[int(p[2])] = (p[3], int(p[4]), int(p[5]))
            self._print(f"OK FAULT ARM {p[2]}")
        elif p == ["FAULT", "CLEAR"]:
            self.ni.fault = [None, None]
            self._print("OK FAULT CLEAR")
        elif p == ["FAULT", "LIST"]:
            for n, e in enumerate(self.ni.fault):
                self._print(f"FAULT {n} " + (f"{e[0]} {e[1]} {e[2]}" if e else "-"))
        elif len(p) == 3 and p[1] == "TRACE" and p[2] in ("ON", "OFF"):
            self.ni.trace = p[2] == "ON"
            self._print("OK FAULT TRACE")
        else:
            self._print(unknown)

    # --- applications -----------------------------------------------------------------------
    def _owners(self, s: str) -> dict:
        payload = self.slot(s)[HEADER_SIZE:]
        return {name: self.apps.get((s, name, payload[a:b])) for name, a, b in REGIONS}

    def _lockup(self):
        if self.iwdg_running and self.iwdg_at is None:
            self.iwdg_at = self.now + self._iwdg_period()
        yield ("halt",)

    def _hardfault(self, own: dict):
        if own["hf"] is None:
            yield from self._lockup()
        self._print("HARDFAULT pc=0x0800A1C4 lr=0x0800A0F7 cfsr=0x00010000 hfsr=0x40000000")
        yield self._flush()
        yield ("reset", "SFT")

    def _app(self, s: str):
        yield ("sleep", self.T.app_init_us)
        own = self._owners(s)
        if own["vec"] is None:
            yield from self._lockup()
        if own["rt"] is None or own["banner"] is None:
            yield from self._hardfault(own)
        info = own["banner"]
        rec = self._meta() if self.design == "ab" else None
        trial = rec is not None and rec.trial == SLOT_NUM[s]
        self.app_t = self.now
        self._print(f"BOOT app {info.version} slot={s} trial={int(trial)} reset={self.reset_cause}")
        self.iwdg_at = None                                   # the main loop feeds the IWDG
        main = own["main"]
        if main is None or main.variant == "crash":
            yield from self._hardfault(own)
        if main.variant == "hang":
            yield from self._lockup()
        start = self.now
        confirm_at = start + int(self.T.confirm_after_s * 1e6) if trial and main.variant != "noconfirm" else None
        deadline_at = start + int(self.T.trial_deadline_s * 1e6) if trial else None
        confirmed = not trial
        while True:
            evs = []
            if not confirmed and confirm_at is not None:
                evs.append(confirm_at)
            if not confirmed and deadline_at is not None:
                evs.append(deadline_at)
            line = yield ("read", max(0, min(evs) - self.now) if evs else None)
            if line is not None:
                yield from self._app_cmd(line.strip(), s, info, own)
                continue
            if confirm_at is not None and not confirmed and self.now >= confirm_at:
                if own["confirm"] is None:
                    yield from self._hardfault(own)
                self._fault_point("CONFIRM", 0)
                t = self.now
                rec = self._meta()
                hdr = parse_header(self.slot(s))
                sc = max(rec.min_security_counter if rec else 0, hdr.security_counter if hdr else 0)
                yield from self._meta_append(4, SLOT_NUM[s], TRIAL_NONE, 0, sc)
                self._trace("CONFIRM", 0, self.now - t)
                self._print("APP confirmed")
                confirmed = True
            elif deadline_at is not None and not confirmed and self.now >= deadline_at:
                deadline_at = None                            # stop feeding: the IWDG resets us
                if self.iwdg_running:
                    self.iwdg_at = self.now + self._iwdg_period()

    def _app_cmd(self, cmd: str, s: str, info, own):
        if not cmd:
            return
        if cmd == "PING":
            self._print("PONG")
        elif cmd == "VER":
            self._print(f"VER {info.version.split('-')[0]}")
        elif cmd == "TICK":
            self._print(f"TICK {(self.now - self.app_t) // 1000}")
        elif cmd == "UID":
            self._print(f"UID {SIM_UID}")
        elif cmd == "STATE":
            rec = self._meta() if self.design == "ab" else None
            if rec:
                self._print(f"STATE active={rec.active_slot} trial={int(rec.trial != TRIAL_NONE)} "
                            f"confirmed={int(rec.trial == TRIAL_NONE)} seq={rec.seq}")
            else:
                self._print(f"STATE active={s} trial=0 confirmed=1 seq=0")
        elif cmd == "RESET":
            self._print("OK RESET")
            yield self._flush()
            yield ("reset", "SFT")
        elif cmd == "CRASH":
            self._print("OK CRASH")
            yield from self._hardfault(own)
        elif cmd == "UPD":
            self._print("OK UPD")
            self.ni.enter_update = True
            yield self._flush()
            yield ("reset", "SFT")
        elif cmd.startswith("FAULT"):
            self._fault_cmd(cmd, bl=False)
        else:
            self._print("ERR unknown command")


# --- host adapters ------------------------------------------------------------------------
class SimSerial:
    def __init__(self, board: SimBoard):
        self.board = board

    def write(self, data: bytes):
        self.board.host_write(data)

    def read_available(self, timeout: float) -> str:
        return self.board.host_read(timeout)

    def close(self):
        pass


class SimClock:
    def __init__(self, board: SimBoard):
        self.board = board

    def now(self) -> float:
        return self.board.now / 1e6

    def sleep(self, s: float) -> None:
        if s > 0:
            self.board.advance(self.board.now + math.ceil(s * 1e6))


class SimProgrammer(bench.Programmer):
    """Fake ST-Link: understands dump_image and mdw, flashes .hex/.bin, resets. Time passes."""
    name = "sim-stlink"

    def __init__(self, board: SimBoard):
        self.board = board
        self.calls = {"debug": 0, "flash": 0, "reset": 0}

    def _spend(self, s: float):
        self.board.advance(self.board.now + int(s * 1e6))

    def debug_commands(self, cmds: list[str], timeout: float = 30) -> str:
        self.calls["debug"] += 1
        self._spend(self.board.T.stlink_read_s)
        out = []
        for c in cmds:
            if m := re.fullmatch(r"dump_image \{(.+)\} (0x[0-9A-Fa-f]+) (\d+)", c):
                Path(m.group(1)).write_bytes(self.board.rd(int(m.group(2), 16), int(m.group(3))))
                out.append(f"dumped {m.group(3)} bytes")
            elif m := re.fullmatch(r"mdw (0x[0-9A-Fa-f]+)(?: (\d+))?", c):
                addr, n = int(m.group(1), 16), int(m.group(2) or 1)
                words = []
                for i in range(n):
                    a = addr + 4 * i
                    if UID_ADDR <= a < UID_ADDR + 12:
                        k = (a - UID_ADDR) // 4
                        words.append(int(SIM_UID[8 * k:8 * k + 8], 16))
                    else:
                        words.append(struct.unpack("<I", self.board.rd(a, 4))[0])
                out.append(f"0x{addr:08x}: " + " ".join(f"{w:08x}" for w in words) + " ")
            else:
                raise bench.BenchError("not_supported", f"[sim] unsupported debug command: {c}")
        return "\n".join(out) + "\n"

    def flash(self, image: Path, under_reset: bool = False) -> str:
        self.calls["flash"] += 1
        image = Path(image)
        if image.suffix.lower() == ".hex":
            segs = ihex_to_segments(image.read_text(encoding="ascii"))
        elif image.suffix.lower() == ".bin":
            segs = [(SECTORS[0][0], image.read_bytes())]
        else:
            raise bench.BenchError("image_invalid", f"[sim] cannot flash {image.name}")
        self._spend(0.05)
        self.board.load_segments(segs)
        self._spend(self.board.T.stlink_flash_s)
        self.board.reset("PIN")
        return f"[sim] {image.name} flashed and verified."

    def reset(self) -> str:
        self.calls["reset"] += 1
        self._spend(self.board.T.stlink_reset_s)
        self.board.reset("PIN")
        return "[sim] board reset."

    def probe_check(self) -> str:
        return "[sim] probe ready."

    def tool_version(self) -> str:
        return "sim"


assert SLOT_NAME[0] == "A"
