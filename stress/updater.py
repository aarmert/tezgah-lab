"""Stop-and-wait host updater (SPEC 4) and the host-side fault injector (cut/corrupt/drop/delay)."""
from __future__ import annotations

import base64
import random
import zlib
from dataclasses import dataclass, field

from .images import Image
from .link import Link
from .proto import BlStatus, ERR_CRC_RE, ERR_SEQ_RE, OK_ERASED_RE, OK_OFF_RE, err_code, fault_arm

BEGIN_TIMEOUT_S = 3.0             # F407 (a 16 KB sector erase); the C031 uses layout.begin_timeout_s (1 s)
REPLY_TIMEOUT_S = 1.0
# SPEC: 1 s. It must stay well below the BL's 3 s inactivity timeout: a lost END has to be retried
# before the BL gives up. A slow END (META1 rotation) is still fine: the late OK COMMIT is accepted
# while the host waits for the retry's reply.
END_TIMEOUT_S = 1.0
# Lines that mean the BL left update mode (reset, gave up, booted): stop the transfer there.
LEFT_UPDATE = ("status", "timeout", "jump", "banner", "wait")
# Boot-flow lines that, when they follow the last 'BL update|resume|nofw', show the BL is no longer
# waiting for UPD commands (e.g. 'BL timeout' -> 'BL jump A' -> 'BOOT app').
LEFT_BEFORE_START = ("status", "timeout", "jump", "banner", "hardfault", "rollback", "repair")
NOISE_RETRYABLE_BEGIN = {"BAD_HDR", "BOARD", "SIZE", "LINK", "LINE"}
# Every ERR code BEGIN can answer (bl_update.c cmd_begin + ERR LINE). Any other reply, e.g. the
# ERR STATE for a DATA line that reached the BL right after a reset, is stale and is skipped.
BEGIN_ERRS = {"BAD_HDR", "BOARD", "SIZE", "LINK", "VERSION", "FLASH", "LINE"}
END_ERRS = {"SHORT", "IMG_CRC", "IMG_SHA", "FLASH", "STATE", "LINE"}


class Injector:
    """Wraps link.send_line. cut_at: transfer byte offset where the host stops for good.
    rate: per-line probability of one noise fault (corrupt one byte, drop the line, or delay it)."""

    def __init__(self, rng: random.Random | None = None, rate: float = 0.0, cut_at: int | None = None,
                 max_delay_s: float = 0.9):
        self.rng = rng or random.Random(0)
        self.rate = rate
        self.cut_at = cut_at
        self.max_delay_s = max_delay_s
        self.counts = {"corrupt": 0, "drop": 0, "delay": 0}
        self.cut_done = False

    @property
    def noisy(self) -> bool:
        return self.rate > 0

    def send(self, link: Link, line: str, span: tuple[int, int] | None = None, prefix_len: int = 0) -> str:
        """span: transfer bytes this line carries; prefix_len: chars before the base64 field."""
        if self.cut_at is not None and span is not None and span[0] <= self.cut_at < span[1]:
            keep = prefix_len + (4 * (self.cut_at - span[0])) // 3
            link.send_raw(line[:keep].encode("ascii"), note=f"cut at transfer byte {self.cut_at}")
            self.cut_done = True
            return "cut"
        if self.rate > 0 and self.rng.random() < self.rate:
            kind = self.rng.choice(("corrupt", "drop", "delay"))
            self.counts[kind] += 1
            if kind == "drop":
                link.note(f"noise: drop {line[:40]}...")
                return "dropped"
            if kind == "delay":
                d = self.rng.uniform(0, self.max_delay_s)
                link.note(f"noise: delay {d * 1000:.0f} ms")
                link.clock.sleep(d)
            else:
                i = self.rng.randrange(len(line))
                c = line[i]
                while c == line[i]:
                    c = chr(self.rng.randint(0x21, 0x7E))
                link.note(f"noise: corrupt char {i}")
                line = line[:i] + c + line[i + 1:]
                link.send_line(line, raw=True)          # ESP bridge: TXH (injected bytes)
                return "sent"
        link.send_line(line)
        return "sent"


@dataclass
class TransferResult:
    status: str = "none"            # commit | err | reset | cut | failed | left (BL had left update
    #                                 mode before the host sent anything)
    err_code: str = ""
    err_line: str = ""
    retries: int = 0
    bytes: int = 0
    t_begin: float | None = None
    t_erased: float | None = None
    t_commit: float | None = None
    erased_us: int | None = None
    erases: int = 0                 # BEGINs that may have erased the target slot
    reset_index: int | None = None  # link.events index of the line where the BL left update mode
    left_kind: str = ""             # kind of that line: status (reset), timeout, jump, banner, wait
    notes: list[str] = field(default_factory=list)

    @property
    def transfer_s(self) -> float | None:
        if self.t_begin is None or self.t_commit is None:
            return None
        return self.t_commit - self.t_begin


class Updater:
    """before_send(kind, index): called right before each protocol line goes out (kind BEGIN, DATA
    with the chunk number, END); the ESP reset source arms its NRST pulse there."""

    def __init__(self, link: Link, inj: Injector | None = None, max_retries: int = 8, before_send=None):
        self.link = link
        self.inj = inj or Injector()
        self.max_retries = max_retries
        self.before_send = before_send

    def _before(self, kind: str, index: int):
        if self.before_send is not None:
            self.before_send(kind, index)

    def _await(self, accept, timeout: float):
        deadline = self.link.clock.now() + timeout
        while True:
            e = self.link.next_event(max(0.0, deadline - self.link.clock.now()))
            if e is None:
                return "timeout", None
            if e.kind in LEFT_UPDATE:
                return "reset", e
            if e.kind == "reply" and accept(e):
                return "reply", e

    def left_update_mode(self) -> int | None:
        """Consume the unread lines. Returns the link.events index of the first boot-flow line after
        the last 'BL update|resume|nofw' (the BL no longer waits for UPD commands), or None."""
        link = self.link
        link.drain()
        idx = None
        for i in range(link.unread_from, len(link.events)):
            k = link.events[i].kind
            if k == "wait":
                idx = None
            elif k in LEFT_BEFORE_START and idx is None:
                idx = i
        return idx

    def transfer(self, image: Image) -> TransferResult:
        r = TransferResult()
        L = image.layout
        HEADER_SIZE, CHUNK = L.header_size, L.chunk
        hdr, payload = image.data[:HEADER_SIZE], image.data[HEADER_SIZE:]
        left = self.left_update_mode()
        if left is not None:
            e = self.link.events[left]
            r.status, r.reset_index, r.left_kind = "left", left, e.kind
            r.notes.append(f"BL had left update mode ({e.line.strip()[:40]}) before the transfer started")
            return r

        def reset(e) -> TransferResult:
            r.status = "reset"
            r.reset_index = self.link.cursor - 1
            r.left_kind = e.kind
            r.notes.append(f"BL left update mode ({e.kind}) at {r.bytes} B")
            return r

        # BEGIN
        line = "UPD BEGIN " + base64.b64encode(hdr).decode()
        tries = 0
        while True:
            self._before("BEGIN", 0)
            if r.t_begin is None:
                r.t_begin = self.link.clock.now()
            how = self.inj.send(self.link, line, (0, HEADER_SIZE), len("UPD BEGIN "))
            if how == "cut":
                r.status = "cut"
                return r
            kind, e = self._await(lambda ev: OK_ERASED_RE.match(ev.line)
                                  or (ev.data[0] == "ERR" and err_code(ev.data[1]) in BEGIN_ERRS),
                                  L.begin_timeout_s)
            if kind == "reset":
                r.erases += 1
                return reset(e)
            if kind == "reply" and (m := OK_ERASED_RE.match(e.line)):
                r.erases += 1
                r.erased_us = int(m.group(1))
                r.t_erased = self.link.clock.now()
                r.bytes = HEADER_SIZE
                break
            if kind == "reply":
                code = err_code(e.data[1])
                retry = code == "LINE" or (self.inj.noisy and code in NOISE_RETRYABLE_BEGIN)
                if not retry:
                    r.status, r.err_code, r.err_line = "err", code, e.line
                    return r
            else:
                r.erases += 1          # no reply: the erase may have run
            tries += 1
            r.retries += 1
            if tries > self.max_retries:
                r.status, r.err_line = "failed", "BEGIN: too many retries"
                return r
            self.link.drain(0.05)

        # DATA
        off, tries = 0, 0
        while off < len(payload):
            chunk = payload[off:off + CHUNK]
            b64 = base64.b64encode(chunk).decode()
            pre = f"UPD DATA {off} "
            line = f"{pre}{b64} {zlib.crc32(chunk):08x}"
            self._before("DATA", off // CHUNK)
            how = self.inj.send(self.link, line, (HEADER_SIZE + off, HEADER_SIZE + off + len(chunk)), len(pre))
            if how == "cut":
                r.status = "cut"
                return r
            want = off
            kind, e = self._await(
                lambda ev: (m := OK_OFF_RE.match(ev.line)) is not None and int(m.group(1)) == want
                or ev.data[0] == "ERR", REPLY_TIMEOUT_S)
            if kind == "reset":
                return reset(e)
            if kind == "reply" and e.data[0] == "OK":
                off += len(chunk)
                r.bytes = HEADER_SIZE + off
                tries = 0
                continue
            if kind == "reply":
                code = err_code(e.data[1])
                if (m := ERR_SEQ_RE.match(e.line)):
                    exp = int(m.group(1))
                    if exp % CHUNK == 0 and exp <= len(payload) and exp != off:
                        r.notes.append(f"ERR SEQ {exp} at {off}: resync")
                        off = exp
                        r.retries += 1
                        tries += 1
                        if tries > self.max_retries:
                            break
                        continue
                elif not (ERR_CRC_RE.match(e.line) or code == "LINE"
                          or (self.inj.noisy and code in ("SEQ", "STATE"))):
                    r.status, r.err_code, r.err_line = "err", code, e.line
                    return r
            tries += 1
            r.retries += 1
            if tries > self.max_retries:
                break
            self.link.drain(0.05)
        if off < len(payload):
            r.status, r.err_line = "failed", f"DATA {off}: too many retries"
            return r

        # END
        tries = 0
        while True:
            self._before("END", 0)
            how = self.inj.send(self.link, "UPD END")
            kind, e = self._await(lambda ev: ev.line.strip() == "OK COMMIT"
                                  or (ev.data[0] == "ERR" and err_code(ev.data[1]) in END_ERRS), END_TIMEOUT_S)
            if kind == "reset":
                return reset(e)
            if kind == "reply" and e.data[0] == "OK":
                r.status = "commit"
                r.t_commit = self.link.clock.now()
                return r
            if kind == "reply":
                code = err_code(e.data[1])
                if code != "LINE":
                    r.status, r.err_code, r.err_line = "err", code, e.line
                    return r
            tries += 1
            r.retries += 1
            if tries > self.max_retries:
                r.status, r.err_line = "failed", "END: too many retries"
                return r
            self.link.drain(0.05)


@dataclass
class EnterResult:
    status: BlStatus | None
    ok: bool
    retries: int
    why: str = ""


def enter_update(link: Link, inj: Injector | None = None, tries: int = 4) -> EnterResult:
    """App 'UPD' -> 'OK UPD' -> reset -> BL status -> 'BL update'."""
    inj = inj or Injector()
    retries = 0
    why = ""
    for _ in range(tries):
        link.drain()
        inj.send(link, "UPD")
        e, _ = link.wait_for(lambda ev: ev.line.strip() in ("OK UPD",) or ev.kind == "status"
                             or ev.line.startswith("ERR"), 1.0)
        if e is None or e.line.startswith("ERR"):
            retries += 1
            why = "no 'OK UPD' from the app" if e is None else e.line
            continue
        status = e.data if e.kind == "status" else None
        e2, seen = link.wait_for(lambda ev: ev.kind in ("wait", "jump", "banner"), 3.0)
        for s in seen:
            if s.kind == "status":
                status = s.data
        if e2 is not None and e2.kind == "wait" and e2.data == "update":
            return EnterResult(status, True, retries)
        why = f"expected 'BL update', got {e2.line if e2 else 'nothing'}"
        return EnterResult(status, False, retries, why)
    return EnterResult(None, False, retries, why)


def arm_faults(link: Link, entries: list[tuple[str, int, int] | None], trace: bool = False) -> str | None:
    """In update mode: FAULT CLEAR, FAULT ARM per entry, optional TRACE ON. Returns an error or None."""
    e = link.command("FAULT CLEAR", r"^(OK FAULT CLEAR|ERR)", 1.0)
    if e is None or not e.line.startswith("OK"):
        return f"FAULT CLEAR failed: {e.line if e else 'no reply'} (firmware without hooks?)"
    for n, ent in enumerate(entries):
        if ent is None:
            continue
        phase, idx, delay = ent
        e = link.command(fault_arm(n, phase, idx, delay), rf"^(OK FAULT ARM {n}|ERR)", 1.0)
        if e is None or not e.line.startswith("OK"):
            return f"FAULT ARM {n} failed: {e.line if e else 'no reply'}"
    if trace:
        e = link.command("FAULT TRACE ON", r"^(OK FAULT TRACE|ERR)", 1.0)
        if e is None or not e.line.startswith("OK"):
            return "FAULT TRACE ON failed"
    return None

