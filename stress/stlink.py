"""ST-Link access through bench.py's Programmer only (SPEC 10).

One OpenOCD invocation dumps SLOT A, SLOT B, META0 and META1 with `dump_image` into a temp dir.
Recovery flashes golden_<design>.bin under reset. bench's flash() passes no load address, so the
.bin is first converted to Intel HEX (a raw .bin would land at address 0 in OpenOCD/CubeProgrammer).
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ._bench import BenchError, bench
from .images import ImageSet, Image, label_candidates, slot_label, slot_valid
from .layout import F407, SLOT_SIZE, Layout
from .meta import MetaRecord, latest


def regions(layout: Layout = F407) -> dict[str, tuple[int, int]]:
    """The four regions one ST-Link read dumps: both slots and both meta areas."""
    return {"A": (layout.slot_base["A"], layout.slot_size), "B": (layout.slot_base["B"], layout.slot_size),
            "META0": layout.meta_region(0), "META1": layout.meta_region(1)}


REGIONS = regions(F407)


@dataclass
class FlashState:
    A: bytes
    B: bytes
    META0: bytes
    META1: bytes
    layout: Layout = field(default=F407, repr=False)

    def slot(self, s: str) -> bytes:
        return self.A if s == "A" else self.B

    def slot_sha(self, s: str) -> str:
        return hashlib.sha256(self.slot(s)).hexdigest()

    def meta(self) -> MetaRecord | None:
        return latest(self.META0, self.META1, self.layout)

    def valid(self, s: str) -> bool:
        return slot_valid(self.slot(s), s, self.layout)[0]

    def labels(self, images: ImageSet, first: list[Image] | None = None) -> dict[str, str]:
        return {s: slot_label(self.slot(s), label_candidates(images, s, first)) for s in ("A", "B")}


def bin_to_ihex(data: bytes, base: int) -> str:
    lines = []

    def rec(addr16: int, typ: int, payload: bytes) -> str:
        b = bytes([len(payload), addr16 >> 8, addr16 & 0xFF, typ]) + payload
        return ":" + b.hex().upper() + f"{(-sum(b)) & 0xFF:02X}"

    upper = None
    for off in range(0, len(data), 16):
        addr = base + off
        if addr >> 16 != upper:
            upper = addr >> 16
            lines.append(rec(0, 4, upper.to_bytes(2, "big")))
        lines.append(rec(addr & 0xFFFF, 0, data[off:off + 16]))
    lines.append(rec(0, 1, b""))
    return "\n".join(lines) + "\n"


def ihex_to_segments(text: str) -> list[tuple[int, bytes]]:
    segs: list[tuple[int, bytearray]] = []
    upper = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        b = bytes.fromhex(line[1:])
        n, addr, typ = b[0], (b[1] << 8) | b[2], b[3]
        if sum(b) & 0xFF:
            raise ValueError("ihex checksum")
        data = b[4:4 + n]
        if typ == 4:
            upper = int.from_bytes(data, "big") << 16
        elif typ == 0:
            a = upper + addr
            if segs and segs[-1][0] + len(segs[-1][1]) == a:
                segs[-1][1].extend(data)
            else:
                segs.append((a, bytearray(data)))
        elif typ == 1:
            break
    return [(a, bytes(d)) for a, d in segs]


class Probe:
    """Wraps a bench Programmer (OpenOCD on hardware, stress.sim.SimProgrammer in --sim)."""

    def __init__(self, prog, cache_dir: Path, layout: Layout = F407):
        self.prog = prog
        self.cache_dir = Path(cache_dir)
        self.layout = layout
        self.reads = 0

    def read_state(self) -> FlashState:
        with tempfile.TemporaryDirectory(prefix="stress_dump_") as td:
            cmds, paths = [], {}
            for name, (addr, size) in regions(self.layout).items():
                p = Path(td) / f"{name}.bin"
                paths[name] = (p, size)
                cmds.append(f"dump_image {{{p.as_posix()}}} 0x{addr:08X} {size}")
            out = self.prog.debug_commands(cmds, timeout=60)
            data = {}
            for name, (p, size) in paths.items():
                if not p.is_file() or p.stat().st_size != size:
                    raise BenchError("stlink_read_failed",
                                     f"dump_image did not produce {name} ({size} bytes).",
                                     ["OpenOCD could not read flash while the target runs; see details",
                                      "Probe or target lost: python bench.py --json recover"],
                                     bench.tail(out))
                data[name] = p.read_bytes()
        self.reads += 1
        return FlashState(**data, layout=self.layout)

    def golden_hex(self, golden_bin: Path, skip_bl: bool = False) -> Path:
        """Intel HEX of a golden .bin. skip_bl: leave out S0 (the BL), so only S1..S4 are erased and
        written (the per-trial metafull baseline must not wear the BL sector)."""
        data = Path(golden_bin).read_bytes()
        L = self.layout
        limit = 0x100000 if L.target == "f407" else L.flash_size
        if len(data) > limit:
            raise BenchError("image_invalid", f"{golden_bin.name} is larger than the {L.target} flash "
                             f"({len(data)} > {limit} bytes).", ["Wrong images folder for --target?"])
        base = L.flash_base
        if skip_bl:
            bl_size = L.bl_size
            data, base = data[bl_size:], L.flash_base + bl_size
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()[:16]
        out = self.cache_dir / f"{Path(golden_bin).stem}{'_nobl' if skip_bl else ''}_{digest}.hex"
        if not out.is_file():
            out.write_text(bin_to_ihex(data, base), encoding="ascii")
        return out

    def flash_golden(self, golden_bin: Path, skip_bl: bool = False) -> str:
        return self.prog.flash(self.golden_hex(golden_bin, skip_bl), under_reset=True)

    def reset(self) -> str:
        return self.prog.reset()

    def read_uid(self) -> str:
        words = bench.parse_mdw(self.prog.debug_commands([f"mdw 0x{self.layout.uid_addr:08X} 3"]))
        if len(words) != 3:
            raise BenchError("tool_error", "UID read returned the wrong number of words.")
        return "".join(f"{v:08X}" for _, v in words)


assert SLOT_SIZE == REGIONS["A"][1]
