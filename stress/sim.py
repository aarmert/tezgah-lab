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
from .images import header_error, naive_header_error, naive_status_word, parse_header, slot_status_word, slot_valid
from .layout import (APP_LINE_MAX, BEGIN_MAX_CHARS, CHUNK, GOLDEN_SIZE, HEADER_SIZE, LINE_MAX_CHARS, PHASES, SECTORS, SLOT_BASE, SLOT_NUM,
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


def timings_from(overrides: dict) -> Timings:
    """Timings with stress.toml [sim] overrides (e.g. stlink_read_s = 3.5)."""
    t = Timings()
    for k, v in (overrides or {}).items():
        if not hasattr(t, k):
            raise bench.BenchError("config_invalid", f"[sim] {k}: no such sim timing.",
                                   ["Valid keys: " + ", ".join(Timings.__dataclass_fields__)])
        setattr(t, k, type(getattr(t, k))(v))
    return t


# RCC_CSR reset flags in the order uart_reset_cause() prints them (hw.c)
CSR_ORDER = ("LPWR", "WWDG", "IWDG", "SFT", "POR", "PIN", "BOR")
RESET_FLAGS = {
    "sw": {"SFT", "PIN"},          # SYSRESETREQ (system_reset, fault ISR); it also pulses NRST
    "iwdg": {"IWDG", "PIN"},
    "pin": {"PIN"},                # ST-Link / NRST
    "por": {"POR", "PIN", "BOR"},  # RCC_CSR reset value
}


def csr_text(flags: set) -> str:
    return ",".join(f for f in CSR_ORDER if f in flags) or "NONE"


@dataclass
class NoInit:
    """noinit.h. `expiry_*` is the record the TIM2 ISR writes (outside the CRC part)."""
    enter_update: bool = False
    update_in_progress: bool = False
    fault: list = field(default_factory=lambda: [None, None])   # (phase, idx, delay_us)
    pending_active: bool = False
    seq: int = 0                                  # pending.seq
    pending: tuple = ("ERASE", 0, 0)              # (phase, idx, delay_us) of the last start
    cancelled_count: int = 0
    cancelled: tuple = ("ERASE", 0, 0)
    expiry_count: int = 0
    expiry_seq: int = 0
    expiry: tuple = ("ERASE", 0, 0)
    trace: bool = False


# --- C parsing helpers (hw.c parse_u32/parse_i32/parse_hex32/next_token, b64.c) ---------------
def _skip1(s: str) -> str:
    return s[1:] if s.startswith(" ") else s


def c_u32(s: str):
    i = 0
    while i < len(s) and "0" <= s[i] <= "9":
        i += 1
    if i == 0 or (i < len(s) and s[i] != " "):
        return None
    v = int(s[:i])
    if v > 0xFFFFFFFF:
        return None
    return v, _skip1(s[i:])


def c_i32(s: str):
    neg = s.startswith("-")
    r = c_u32(s[1:] if neg else s)
    if r is None or r[0] > 0x7FFFFFFF:
        return None
    return (-r[0] if neg else r[0]), r[1]


def c_hex32(s: str):
    i = 0
    while i < len(s) and s[i] in "0123456789abcdefABCDEF":
        if i >= 8:
            return None
        i += 1
    if i == 0 or (i < len(s) and s[i] != " "):
        return None
    return int(s[:i], 16), _skip1(s[i:])


def c_token(s: str):
    i = s.find(" ")
    if i < 0:
        return s, ""
    return s[:i], _skip1(s[i:])


B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


def c_b64(text: str, out_max: int) -> bytes | None:
    """b64.c b64_decode: None where the C function returns -1."""
    n = len(text)
    if n % 4:
        return None
    out = bytearray()
    for i in range(0, n, 4):
        v, pad = [], 0
        for k in range(4):
            c = text[i + k]
            if c == "=":
                if i + 4 != n or k < 2:
                    return None
                v.append(0)
                pad += 1
            else:
                if pad or c not in B64:
                    return None
                v.append(B64[c])
        w = (v[0] << 18) | (v[1] << 12) | (v[2] << 6) | v[3]
        cnt = 3 - pad
        if len(out) + cnt > out_max:
            return None
        out += bytes([(w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF][:cnt])
    return bytes(out)


def slot_letter(n: int) -> str:
    return SLOT_NAME.get(n, "-")


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
        self.nrst_at = None                 # external NRST pulse (a glitch; tests only)
        self.boot_t = 0
        self.app_t = 0
        self.csr: set = set()               # RCC_CSR reset flags (cleared by the BL)
        self.reset_csr: set = set()         # noinit.reset_csr: what the BL saw
        self.snap = (0, None, 0, None)      # fault report snapshot taken at BL entry
        self.design = "?"
        self.resets = 0
        if golden is not None:
            self.mem[:len(golden)] = golden
        self.reset("por")

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
    def reset(self, kind: str):
        """kind: sw (SYSRESETREQ), iwdg, pin (ST-Link/NRST), por."""
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
        self.tim2_at = None
        self.iwdg_running, self.iwdg_at = False, None
        self.boot_t = self.now
        self.csr |= RESET_FLAGS[kind]
        if kind == "por":
            self.ni = NoInit()
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
            if self.nrst_at is not None:
                cands.append((self.nrst_at, 4))
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
            if kind == 0:                     # TIM2 ISR: record the expiry, then SYSRESETREQ
                ni = self.ni
                ni.expiry_count += 1
                ni.expiry_seq, ni.expiry = ni.seq, ni.pending
                self.reset("sw")
            elif kind == 1:
                self.reset("iwdg")
            elif kind == 4:
                self.nrst_at = None
                self.reset("pin")
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

    # --- fault hook (fault.c) --------------------------------------------------------------
    def _fp(self, phase: str, idx: int):
        """fault_point(): a matching armed entry is disarmed and starts the timer. delay 0 resets
        on the spot (the zero-length sleep lets the scheduler take the timer first), delay 1
        behaves like 2 (TIM2 ARR >= 1)."""
        ni = self.ni
        if self.hooks and self.tim2_at is None:
            for n in (0, 1):
                e = ni.fault[n]
                if e and e[0] == phase and (e[1] < 0 or e[1] == idx):
                    ni.fault[n] = None                     # disarm in noinit first
                    ni.pending_active = True
                    ni.seq += 1
                    ni.pending = (phase, idx, e[2])
                    self.tim2_at = self.now + (0 if e[2] == 0 else max(2, e[2]))
                    break
        yield ("sleep", 0)

    def _cancel_pending(self):
        ni = self.ni
        ni.cancelled_count += 1
        ni.cancelled = ni.pending
        ni.pending_active = False

    def _boot_snapshot(self):
        """fault_boot_snapshot(): the last start either expired (fired) or not (cancelled)."""
        if not self.hooks:
            self.snap = (0, None, 0, None)
            return
        ni = self.ni
        if ni.pending_active:
            if ni.expiry_count and ni.expiry_seq == ni.seq:
                ni.pending_active = False
            else:
                self._cancel_pending()
        self.snap = (ni.expiry_count, ni.expiry, ni.cancelled_count, ni.cancelled)

    def _print_report(self):
        """fault_print_report(): print, then clear only what was reported."""
        fc, finfo, cc, cinfo = self.snap
        if fc:
            self._print(f"BL fault_fired {fc} {finfo[0]} {finfo[1]} {finfo[2]}")
        if cc:
            self._print(f"BL fault_cancelled {cc} {cinfo[0]} {cinfo[1]} {cinfo[2]}")
        self.ni.expiry_count -= fc
        self.ni.cancelled_count -= cc
        self.snap = (0, None, 0, None)

    def _fault_cmd(self, line: str) -> bool:
        """fault_command(): False when the line is not a FAULT command (or there are no hooks)."""
        if not self.hooks or not (line == "FAULT" or line.startswith("FAULT ")):
            return False
        ni = self.ni
        p = _skip1(line[5:])
        if p.startswith("ARM "):
            r = c_u32(p[4:])
            if r and r[0] <= 1:
                n, rest = r
                ph, rest = c_token(rest)
                r = c_i32(rest) if ph in PHASES else None
                if r and r[0] >= -1:
                    idx, rest = r
                    r = c_u32(rest)
                    if r and r[1] in ("", "reset", "RESET"):
                        ni.fault[n] = (ph, idx, r[0])
                        self._print(f"OK FAULT ARM {n}")
                        return True
        elif p == "CLEAR":
            if self.tim2_at is not None:
                self.tim2_at = None
                if ni.pending_active:
                    self._cancel_pending()
            ni.fault = [None, None]
            self._print("OK FAULT CLEAR")
            return True
        elif p == "LIST":
            for n, e in enumerate(ni.fault):
                self._print(f"FAULT {n} " + (f"{e[0]} {e[1]} {e[2]}" if e else "-"))
            return True
        elif p in ("TRACE ON", "TRACE OFF"):
            ni.trace = p == "TRACE ON"
            self._print("OK FAULT TRACE")
            return True
        self._print("ERR FAULT")
        return True

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

    def _naive_bootable(self) -> bool:
        return looks_like_sp(struct.unpack("<I", self.slot("A")[HEADER_SIZE:HEADER_SIZE + 4])[0])

    # --- bootloader (bl.c) -------------------------------------------------------------------
    def _boot(self):
        yield ("sleep", self.T.reset_us + self.T.bl_init_us)
        bl = self.rd(SECTORS[0][0], 32)
        design = next((d for d, m in BL_MARK.items() if bl.startswith(m)), None)
        if design is None:
            yield ("halt",)                                  # blank or unknown BL: silent
        self.design = design
        self._boot_snapshot()                                # before BOOT: nothing is lost
        t0 = self.now
        yield from self._fp("BOOT", 0)
        self.reset_csr, self.csr = set(self.csr), set()      # RMVF
        if design == "ab":
            yield from self._bl_ab(t0)
        else:
            yield from self._bl_naive(t0)
        yield ("halt",)

    def _status(self, design: str) -> str:
        if design == "ab":
            a = slot_status_word(self.slot("A"), "A")
            b = slot_status_word(self.slot("B"), "B")
            rec = self._meta()
            active = slot_letter(rec.active) if rec else "-"
            state = ("trial" if rec.trial != TRIAL_NONE else "confirmed") if rec else "none"
            att, seq = (rec.attempts, rec.seq) if rec else (0, 0)
            e0, e1 = rec.erases() if rec else (0, 0)
        else:
            a, b, state, att, seq, e0, e1 = naive_status_word(self.slot("A")), "-", "none", 0, 0, 0, 0
            active = "A" if self._naive_bootable() else "-"
        return (f"BL v0.1 design={design}{' hooks' if self.hooks else ''} target=f407 slotA={a} slotB={b} "
                f"active={active} state={state} attempts={att} seq={seq} erases={e0},{e1} "
                f"reset={csr_text(self.reset_csr)}")

    def _refresh(self):
        yield ("sleep", self._crc_cost("A") + self._crc_cost("B"))

    def _bl_ab(self, t0: int):
        yield from self._refresh()
        self._print(self._status("ab"))
        self._print_report()
        self._trace("BOOT", 0, self.now - t0)
        ni = self.ni
        while True:                                           # boot_decision()
            valid = {s: self._valid(s) for s in "AB"}
            rec = self._meta()
            # 1. no valid meta record: record whichever slot is valid (A first)
            if rec is None:
                pick = next((s for s in "AB" if valid[s][0]), None)
                if pick is None:
                    self._print("BL nofw")
                    yield from self._update_mode(timeout=False)
                    yield from self._refresh()
                    continue
                yield from self._meta_append(1, SLOT_NUM[pick], TRIAL_NONE, 0, valid[pick][1].security_counter)
                rec = self._meta()
            # 2. noinit flags
            if ni.enter_update:
                ni.enter_update = False
                self._print("BL update")
                yield from self._update_mode(timeout=True)
                yield from self._refresh()
                continue
            if ni.update_in_progress:
                self._print("BL resume")
                yield from self._update_mode(timeout=True)
                yield from self._refresh()
                continue
            # 3. trial pending
            if rec.trial != TRIAL_NONE:
                t = rec.trial
                if t > 1 or rec.attempts >= 3 or not valid[SLOT_NAME[t]][0]:
                    yield from self._meta_append(5, rec.active, TRIAL_NONE, rec.attempts, rec.min_security_counter)
                    self._print(f"BL rollback {slot_letter(t)}->{slot_letter(rec.active)}")
                    rec = self._meta()
                else:
                    yield from self._meta_append(3, rec.active, t, rec.attempts + 1, rec.min_security_counter)
                    self.iwdg_running = True
                    self.iwdg_at = self.now + self._iwdg_period()
                    self._print(f"BL jump {slot_letter(t)} trial")
                    yield from self._jump(SLOT_NAME[t])
            # 4. boot active, or repair to the other valid slot
            a = rec.active
            if a <= 1 and valid[SLOT_NAME[a]][0]:
                self._print(f"BL jump {slot_letter(a)}")
                yield from self._jump(SLOT_NAME[a])
            if a <= 1:
                other = 1 - a if valid[SLOT_NAME[1 - a]][0] else -1
            else:
                other = 0 if valid["A"][0] else (1 if valid["B"][0] else -1)
            if other >= 0:
                yield from self._meta_append(6, other, TRIAL_NONE, 0, rec.min_security_counter)
                self._print(f"BL repair active={slot_letter(other)}")
                self._print(f"BL jump {slot_letter(other)}")
                yield from self._jump(SLOT_NAME[other])
            self._print("BL nofw")
            yield from self._update_mode(timeout=False)
            yield from self._refresh()

    def _bl_naive(self, t0: int):
        yield ("sleep", 200)
        self._print(self._status("naive"))
        self._print_report()
        self._trace("BOOT", 0, self.now - t0)
        while True:
            if self.ni.enter_update:
                self.ni.enter_update = False
                self._print("BL update")
                yield from self._update_mode(timeout=True)
                continue
            # Deliberately unsafe: no CRC, no header check, no resume. Word 0 of the payload decides.
            if self._naive_bootable():
                self._print("BL jump A")
                yield from self._jump("A")
            self._print("BL nofw")
            yield from self._update_mode(timeout=False)

    def _jump(self, s: str):
        yield from self._fp("JUMP", SLOT_NUM[s])
        t = self.now
        yield ("sleep", 20)
        self._trace("JUMP", SLOT_NUM[s], self.now - t)
        yield from self._app(s)
        yield ("halt",)

    def _meta_append(self, kind: int, active: int, trial: int, attempts: int, min_sc: int):
        """meta.c meta_append()."""
        rec = self._meta()
        cur = rec.sector if rec else 1
        seq = rec.seq + 1 if rec else 1
        e = dict(zip((1, 4), rec.erases())) if rec else {1: 0, 4: 0}
        idx = free_index(self.sector(cur))
        if idx is None:
            other = other_meta(cur)
            yield from self._fp("ROTATE", other)
            op = self._erase_op(other)
            yield op
            self._trace("ROTATE", other, op[1])
            e[other] += 1
            cur, idx = other, 0
        r = pack_record(seq, kind, active, trial, attempts, min_sc, e[cur], e[other_meta(cur)])
        addr = SECTORS[cur][0] + idx * 32
        yield from self._fp("META", kind)
        t = self.now
        yield self._program_op(addr, r[:28])
        yield self._program_op(addr + 28, r[28:])          # CRC word last
        self._trace("META", kind, self.now - t)

    # --- update mode (bl_update.c) -------------------------------------------------------------
    def _update_mode(self, timeout: bool):
        ab = self.design == "ab"
        ni = self.ni
        sess = None
        while True:
            line = yield ("read", int(self.T.update_timeout_s * 1e6) if timeout else None)
            if line is None:
                sess = None
                if ab:
                    ni.update_in_progress = False
                self._print("BL timeout")
                return
            if not line:
                continue                                      # uart_getline skips empty lines
            if len(line) > BEGIN_MAX_CHARS or (len(line) > LINE_MAX_CHARS and not line.startswith("UPD BEGIN ")):
                self._print("ERR LINE")
            elif line.startswith("UPD DATA "):
                yield from self._upd_data(sess, line[9:])
            elif line.startswith("UPD BEGIN "):
                sess = yield from self._upd_begin(line[10:])
            elif line == "UPD END":
                if sess is None:
                    self._print("ERR STATE")
                else:
                    yield from self._upd_end(sess)            # returns only on an error
                    sess = None
            elif line == "UPD ABORT":
                sess = None
                if ab:
                    ni.update_in_progress = False
                self._print("OK ABORT")
                return                                        # no reset: the boot decision goes on
            elif line == "PING":
                self._print("PONG")
            elif self._fault_cmd(line):
                pass
            else:
                self._print("ERR LINE")

    def _upd_begin(self, b64: str):
        raw = None if " " in b64 else c_b64(b64, HEADER_SIZE)
        if raw is None or len(raw) != HEADER_SIZE:
            self._print("ERR BAD_HDR")
            return None
        h = parse_header(raw)
        ab = self.design == "ab"
        if ab:
            rec = self._meta()
            target = "B" if rec is not None and rec.active == 0 else "A"
            code = header_error(h, target)
            if code is None and h.security_counter < (rec.min_security_counter if rec else 0):
                code = "VERSION"
        else:
            target, code = "A", naive_header_error(h)
        if code:
            self._print(f"ERR {code}")
            return None
        if ab:
            self.ni.update_in_progress = True
        sector = SLOT_SECTOR[target]
        yield from self._fp("ERASE", sector)
        op = self._erase_op(sector)
        yield op
        self._trace("ERASE", sector, op[1])
        self._print(f"OK ERASED {op[1]}")
        return {"h": h, "raw": raw, "target": target, "written": 0, "chunks": 0,
                "base": SLOT_BASE[target] + HEADER_SIZE}

    def _upd_data(self, s: dict | None, rest: str):
        """cmd_data(): LINE -> STATE -> CRC -> SEQ -> SIZE -> idempotent retry -> program."""
        r = c_u32(rest)
        if r is None:
            self._print("ERR LINE")
            return
        off, rest = r
        b64, rest = c_token(rest)
        r = c_hex32(rest) if b64 else None
        if r is None or r[1]:
            self._print("ERR LINE")
            return
        crc = r[0]
        if s is None:
            self._print("ERR STATE")
            return
        data = c_b64(b64, CHUNK)
        if not data or zlib.crc32(data) != crc:
            self._print(f"ERR CRC {off}")
            return
        if off % 4 or off > s["written"]:
            self._print(f"ERR SEQ {s['written']}")
            return
        if len(data) % 4 or off + len(data) > s["h"].payload_size:
            self._print("ERR SIZE")
            return
        if off < s["written"]:
            same = off + len(data) <= s["written"] and self.rd(s["base"] + off, len(data)) == data
            self._print(f"OK {off}" if same else f"ERR SEQ {s['written']}")
            return
        k = s["chunks"]
        yield from self._fp("DATA", k)
        op = self._program_op(s["base"] + off, data)
        yield op
        self._trace("DATA", k, op[1])
        s["written"] += len(data)
        s["chunks"] += 1
        self._print(f"OK {off}")

    def _upd_end(self, s: dict):
        h, target = s["h"], s["target"]
        slot_base = SLOT_BASE[target]
        ab = self.design == "ab"

        def fail(line: str):
            if ab:
                self.ni.update_in_progress = False
            self._print(line)

        if ab:
            if s["written"] < h.payload_size:
                fail("ERR SHORT")
                return
            payload = self.rd(slot_base + HEADER_SIZE, h.payload_size)
            yield ("sleep", self.T.crc_us_per_kb * h.payload_size // 1024)
            if zlib.crc32(payload) != h.payload_crc32:
                fail("ERR IMG_CRC")
                return
            yield ("sleep", self.T.sha_us_per_kb * h.payload_size // 1024)
            if hashlib.sha256(payload).digest() != h.payload_sha256:
                fail("ERR IMG_SHA")
                return
        yield from self._fp("HDR", 0)
        op = self._program_op(slot_base, s["raw"])
        yield op
        self._trace("HDR", 0, op[1])
        if ab:
            rec = self._meta()
            yield from self._meta_append(2, rec.active if rec else TRIAL_NONE, SLOT_NUM[target], 0,
                                         rec.min_security_counter if rec else 0)
            self.ni.update_in_progress = False
        self._print("OK COMMIT")
        yield self._flush()
        yield ("reset", "sw")

    # --- applications (app_rt.c, app.c) ------------------------------------------------------
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
        yield ("reset", "sw")

    def _app(self, s: str):
        yield ("sleep", self.T.app_init_us)
        own = self._owners(s)
        if own["vec"] is None:
            yield from self._lockup()
        if own["rt"] is None or own["banner"] is None:
            yield from self._hardfault(own)
        info = own["banner"]
        rec = self._meta()
        trial = rec is not None and rec.trial == SLOT_NUM[s]
        self.app_t = self.now
        self._print(f"BOOT app {info.version} slot={s} trial={int(trial)} reset={csr_text(self.reset_csr)}")
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
                if len(line) > APP_LINE_MAX:
                    self._print("ERR line too long")
                elif line:
                    yield from self._app_cmd(line.upper(), s, info, own)
                continue
            if confirm_at is not None and not confirmed and self.now >= confirm_at:
                if own["confirm"] is None:
                    yield from self._hardfault(own)
                t = self.now
                yield from self._fp("CONFIRM", 0)
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
        if cmd == "PING":
            self._print("PONG")
        elif cmd == "VER":
            self._print(f"VER {info.version.split('-')[0]}")
        elif cmd == "TICK":
            self._print(f"TICK {(self.now - self.app_t) // 1000}")
        elif cmd == "UID":
            self._print(f"UID {SIM_UID}")
        elif cmd == "RESET":
            self._print("OK RESET")
            yield self._flush()
            yield ("reset", "sw")
        elif cmd == "CRASH":
            self._print("OK CRASH")
            yield from self._hardfault(own)
        elif cmd == "UPD":
            self.ni.enter_update = True
            self._print("OK UPD")
            yield self._flush()
            yield ("reset", "sw")
        elif cmd == "STATE":
            rec = self._meta()
            trial = rec is not None and rec.trial != TRIAL_NONE
            confirmed = rec is not None and not trial and rec.active == SLOT_NUM[s]
            self._print(f"STATE active={slot_letter(rec.active) if rec else '-'} trial={int(trial)} "
                        f"confirmed={int(confirmed)} seq={rec.seq if rec else 0}")
        elif self._fault_cmd(cmd):
            pass
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
        self.board.reset("pin")
        return f"[sim] {image.name} flashed and verified."

    def reset(self) -> str:
        self.calls["reset"] += 1
        self._spend(self.board.T.stlink_reset_s)
        self.board.reset("pin")
        return "[sim] board reset."

    def probe_check(self) -> str:
        return "[sim] probe ready."

    def tool_version(self) -> str:
        return "sim"


assert SLOT_NAME[0] == "A"
