"""META record format and log scan (SPEC 4.1). Used by the host (ST-Link reads) and the sim BL."""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .layout import META_MAGIC, META_RECORD, TRIAL_NONE, KIND, SLOT_NAME

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
    sector: int = 0          # 1 = META0, 4 = META1
    index: int = 0

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
        if self.sector == 4:
            return self.erase_other, self.erase_this
        return self.erase_this, self.erase_other


def pack_record(seq: int, kind: int, active: int, trial: int, attempts: int, min_sc: int,
                erase_this: int, erase_other: int) -> bytes:
    body = struct.pack(REC_FMT, META_MAGIC, seq, kind, active, trial, attempts, min_sc,
                       erase_this, erase_other, 0xFFFFFFFF)
    return body + struct.pack("<I", zlib.crc32(body))


def unpack_record(b: bytes, sector: int = 0, index: int = 0) -> MetaRecord | None:
    if len(b) < META_RECORD:
        return None
    f = struct.unpack_from(REC_FMT, b, 0)
    (crc,) = struct.unpack_from("<I", b, 28)
    if f[0] != META_MAGIC or zlib.crc32(b[:28]) != crc:
        return None
    return MetaRecord(f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], sector, index)


MAGIC_BYTES = struct.pack("<I", META_MAGIC)


def scan_sector(data: bytes, sector: int) -> list[MetaRecord]:
    data = bytes(data)
    out = []
    i = data.find(MAGIC_BYTES)
    while i >= 0:
        if i % META_RECORD == 0:
            r = unpack_record(data[i:i + META_RECORD], sector, i // META_RECORD)
            if r is not None:
                out.append(r)
        i = data.find(MAGIC_BYTES, i + 1)
    return out


def free_index(data: bytes) -> int | None:
    """First record slot after the last non-erased one (a half-written record still uses its slot)."""
    used = len(bytes(data).rstrip(b"\xff"))
    idx = (used + META_RECORD - 1) // META_RECORD
    return idx if idx < len(data) // META_RECORD else None


def latest(meta0: bytes, meta1: bytes) -> MetaRecord | None:
    recs = scan_sector(meta0, 1) + scan_sector(meta1, 4)
    return max(recs, key=lambda r: r.seq) if recs else None
