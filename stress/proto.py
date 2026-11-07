"""Serial line regexes and parsers (SPEC section 3, 4, 6). This module is the parsing contract.

Status and banner regexes use search(), not match(): when a reset cuts a line in half, the next
line arrives glued to the fragment ("BOOT apBL v0.1 ...").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VER = r"\d+\.\d+\.\d+"

# --- bootloader -----------------------------------------------------------
# `target=` (SPEC-C031C6 3) is optional: builds before it print no target, which means f407.
BL_STATUS_RE = re.compile(
    rf"BL v0\.1 design=(ab|naive)( hooks)?(?: target=(f407|c031))? slotA=(ok:{VER}|bad|empty) "
    rf"slotB=(ok:{VER}|bad|empty|-) "
    r"active=(A|B|-) state=(confirmed|trial|none) attempts=(\d+) seq=(\d+) erases=(\d+),(\d+) reset=(\S+)")
BL_JUMP_RE = re.compile(r"BL jump (A|B)( trial)?$")
BL_ROLLBACK_RE = re.compile(r"BL rollback (A|B)->(A|B)$")
BL_REPAIR_RE = re.compile(r"BL repair active=(A|B)$")
BL_WAIT_RE = re.compile(r"BL (update|resume|nofw)$")
BL_TIMEOUT_RE = re.compile(r"BL timeout$")
BL_FIRED_RE = re.compile(r"BL fault_fired (\d+) ([A-Z]+) (-?\d+) (\d+)$")
BL_CANCELLED_RE = re.compile(r"BL fault_cancelled (\d+) ([A-Z]+) (-?\d+) (\d+)$")

# --- application ----------------------------------------------------------
# SPEC: BOOT app (\d+\.\d+\.\d+)(?:-\w+)? ... ; the suffix is captured here (same language).
APP_BOOT_RE = re.compile(rf"BOOT app ({VER})(?:-(\w+))? slot=(A|B) trial=(0|1) reset=(\S+)")
APP_CONFIRMED_RE = re.compile(r"^APP confirmed$")
HARDFAULT_RE = re.compile(r"HARDFAULT pc=0x([0-9A-Fa-f]{8}) lr=0x([0-9A-Fa-f]{8}) "
                          r"cfsr=0x([0-9A-Fa-f]{8}) hfsr=0x([0-9A-Fa-f]{8})")

# --- replies ----------------------------------------------------------------
REPLY_RE = re.compile(r"^(OK|ERR)(?: (.*))?$")
OK_ERASED_RE = re.compile(r"^OK ERASED (\d+)$")
OK_OFF_RE = re.compile(r"^OK (\d+)$")
ERR_CRC_RE = re.compile(r"^ERR CRC (\d+)$")
ERR_SEQ_RE = re.compile(r"^ERR SEQ (\d+)$")
TRACE_RE = re.compile(r"^T ([A-Z]+) (-?\d+) (\d+)$")
FAULT_LIST_RE = re.compile(r"^FAULT ([01]) (?:-|([A-Z]+) (-?\d+) (\d+))$")

ERR_CODES = ("BAD_HDR", "BOARD", "SIZE", "LINK", "VERSION", "CRC", "SEQ", "SHORT",
             "IMG_CRC", "IMG_SHA", "LINE", "STATE", "FLASH")


@dataclass
class BlStatus:
    design: str
    hooks: bool
    slotA: str
    slotB: str
    active: str
    state: str
    attempts: int
    seq: int
    erases: tuple[int, int]
    reset: str
    line: str
    target: str = "f407"


@dataclass
class Banner:
    version: str          # "2.0.0"
    suffix: str           # "crash" or ""
    slot: str
    trial: bool
    reset: str
    line: str

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.suffix}" if self.suffix else self.version


@dataclass
class Event:
    kind: str             # status|jump|rollback|repair|wait|timeout|fired|cancelled|banner|confirmed|hardfault|
    #                       trace|reply|other
    line: str
    t: float = 0.0
    data: object = None


def parse_status(line: str) -> BlStatus | None:
    m = BL_STATUS_RE.search(line)
    if not m:
        return None
    return BlStatus(m.group(1), bool(m.group(2)), m.group(4), m.group(5), m.group(6), m.group(7),
                    int(m.group(8)), int(m.group(9)), (int(m.group(10)), int(m.group(11))), m.group(12),
                    line[m.start():], m.group(3) or "f407")


def parse_banner(line: str) -> Banner | None:
    m = APP_BOOT_RE.search(line)
    if not m:
        return None
    return Banner(m.group(1), m.group(2) or "", m.group(3), m.group(4) == "1", m.group(5), line[m.start():])


def parse_line(line: str, t: float = 0.0) -> Event:
    """Classify one received line. Order matters: a glued fragment is classified by what it ends with."""
    if (st := parse_status(line)) is not None:
        return Event("status", line, t, st)
    if (b := parse_banner(line)) is not None:
        return Event("banner", line, t, b)
    s = line.strip()
    if m := BL_JUMP_RE.search(s):
        return Event("jump", line, t, (m.group(1), bool(m.group(2))))
    if m := BL_ROLLBACK_RE.search(s):
        return Event("rollback", line, t, (m.group(1), m.group(2)))
    if m := BL_REPAIR_RE.search(s):
        return Event("repair", line, t, m.group(1))
    if m := BL_WAIT_RE.search(s):
        return Event("wait", line, t, m.group(1))
    if BL_TIMEOUT_RE.search(s):
        return Event("timeout", line, t)
    if m := BL_FIRED_RE.search(s):
        return Event("fired", line, t, (int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))))
    if m := BL_CANCELLED_RE.search(s):
        return Event("cancelled", line, t, (int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))))
    if APP_CONFIRMED_RE.match(s):
        return Event("confirmed", line, t)
    if m := HARDFAULT_RE.search(s):
        return Event("hardfault", line, t, m.groups())
    if m := TRACE_RE.match(s):
        return Event("trace", line, t, (m.group(1), int(m.group(2)), int(m.group(3))))
    if m := REPLY_RE.match(s):
        return Event("reply", line, t, (m.group(1), m.group(2) or ""))
    return Event("other", line, t)


def err_code(reply_rest: str) -> str:
    """'CRC 192' -> 'CRC', 'IMG_CRC' -> 'IMG_CRC'."""
    return reply_rest.split()[0] if reply_rest else ""


# --- host -> BL lines ---------------------------------------------------------
def fault_arm(n: int, phase: str, index: int, delay_us: int) -> str:
    return f"FAULT ARM {n} {phase} {index} {delay_us}"
