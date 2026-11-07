"""META record format and log scan (SPEC 4.1). Used by the host (ST-Link reads) and the sim BL."""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .layout import F407, META_MAGIC, META_RECORD, TRIAL_NONE, KIND, SLOT_NAME, Layout

REC_FMT = "<IIBBBBIIII"     # 28 bytes before the CRC


@dataclass
class MetaRecord:
    seq: int
    kind: int
    active: int
    trial: int
    attempts: int
    min_security_counter: int
    erase_this: int
    erase_other: int
    sector: int = 0          # erase unit: F407 1 = META0, 4 = META1; C031 page 6 / 7
    index: int = 0
    meta1: bool = False      # the record lives in META1

    @property
    def kind_name(self) -> str:
        return KIND.get(self.kind, f"?{self.kind}")

    @property
    def active_slot(self) -> str:
        return SLOT_NAME.get(self.active, "?")

    @property
    def trial_slot(self) -> str | None:
        return None if self.trial == TRIAL_NONE else SLOT_NAME.get(self.trial, "?")

    @property
    def state(self) -> str:
        return "confirmed" if self.trial == TRIAL_NONE else "trial"

    def erases(self) -> tuple[int, int]:
        """(META0 erase count, META1 erase count)."""
        if self.meta1:
            return self.erase_other, self.erase_this
        return self.erase_this, self.erase_other


def pack_record(seq: int, kind: int, active: int, trial: int, attempts: int, min_sc: int,
                erase_this: int, erase_other: int) -> bytes:
    body = struct.pack(REC_FMT, META_MAGIC, seq, kind, active, trial, attempts, min_sc,
                       erase_this, erase_other, 0xFFFFFFFF)
    return body + struct.pack("<I", zlib.crc32(body))


def unpack_record(b: bytes, sector: int = 0, index: int = 0, meta1: bool | None = None) -> MetaRecord | None:
    if len(b) < META_RECORD:
        return None
    f = struct.unpack_from(REC_FMT, b, 0)
    (crc,) = struct.unpack_from("<I", b, 28)
    if f[0] != META_MAGIC or zlib.crc32(b[:28]) != crc:
        return None
    return MetaRecord(f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], sector, index,
                      sector == 4 if meta1 is None else meta1)


MAGIC_BYTES = struct.pack("<I", META_MAGIC)


def scan_sector(data: bytes, sector: int, meta1: bool | None = None) -> list[MetaRecord]:
    data = bytes(data)
    out = []
    i = data.find(MAGIC_BYTES)
    while i >= 0:
        if i % META_RECORD == 0:
            r = unpack_record(data[i:i + META_RECORD], sector, i // META_RECORD, meta1)
            if r is not None:
                out.append(r)
        i = data.find(MAGIC_BYTES, i + 1)
    return out


def free_index(data: bytes) -> int | None:
    """First record slot after the last non-erased one (a half-written record still uses its slot)."""
    used = len(bytes(data).rstrip(b"\xff"))
    idx = (used + META_RECORD - 1) // META_RECORD
    return idx if idx < len(data) // META_RECORD else None


def latest(meta0: bytes, meta1: bytes, layout: Layout = F407) -> MetaRecord | None:
    u0, u1 = layout.meta_units
    recs = scan_sector(meta0, u0, False) + scan_sector(meta1, u1, True)
    return max(recs, key=lambda r: r.seq) if recs else None
