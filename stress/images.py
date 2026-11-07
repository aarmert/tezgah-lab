"""Image header format (SPEC 2), the image set + manifest, and slot match labels."""
from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .layout import F407, HEADER_VERSION, IMAGE_MAGIC, Layout, version_str

HDR_FMT = "<IHHIIIIII32s"          # 64 bytes
NAME_RE = re.compile(r"^(?P<base>[a-z0-9_]+?)_(?P<slot>[AB])\.img$")
APP_VARIANTS = ("crash", "hang", "noconfirm")
GOLDEN_KEYS = ("ab", "naive", "ab_metafull")   # golden_<key>.bin


@dataclass
class Header:
    magic: int
    header_version: int
    flags: int
    board_id: int
    fw_version: int
    security_counter: int
    link_addr: int
    payload_size: int
    payload_crc32: int
    payload_sha256: bytes
    header_crc32: int
    raw: bytes
    crc_off: int = F407.header_crc_off

    @property
    def version(self) -> str:
        return version_str(self.fw_version)

    def crc_ok(self) -> bool:
        return zlib.crc32(self.raw[:self.crc_off]) == self.header_crc32


def build_header(*, fw_version: int, security_counter: int, link_addr: int, payload: bytes,
                 board_id: int | None = None, payload_size: int | None = None, flags: int = 0,
                 magic: int = IMAGE_MAGIC, layout: Layout = F407) -> bytes:
    size = len(payload) if payload_size is None else payload_size
    bid = layout.board_id if board_id is None else board_id
    first = struct.pack(HDR_FMT, magic, HEADER_VERSION, flags, bid, fw_version, security_counter,
                        link_addr, size, zlib.crc32(payload), hashlib.sha256(payload).digest())
    body = first + b"\xff" * (layout.header_crc_off - len(first))
    return body + struct.pack("<I", zlib.crc32(body))


def parse_header(b: bytes, layout: Layout = F407) -> Header | None:
    if len(b) < layout.header_size:
        return None
    f = struct.unpack_from(HDR_FMT, b, 0)
    (hcrc,) = struct.unpack_from("<I", b, layout.header_crc_off)
    return Header(*f, header_crc32=hcrc, raw=bytes(b[:layout.header_size]), crc_off=layout.header_crc_off)


def header_error(h: Header | None, slot: str | None = None, *, check_link: bool = True,
                 layout: Layout = F407) -> str | None:
    """The BL's BEGIN-time header check, in the BL's order. Returns an ERR code or None."""
    if h is None or h.magic != IMAGE_MAGIC or h.header_version != HEADER_VERSION or not h.crc_ok():
        return "BAD_HDR"
    if h.board_id != layout.board_id:
        return "BOARD"
    if h.payload_size > layout.max_payload or h.payload_size % layout.align or h.payload_size == 0:
        return "SIZE"
    if check_link and slot is not None and h.link_addr != layout.payload_addr(slot):
        return "LINK"
    return None


def slot_valid(slot_bytes: bytes, slot: str, layout: Layout = F407) -> tuple[bool, Header | None]:
    """SPEC 2 validity: magic, header CRC, board, link_addr, size and payload CRC32."""
    h = parse_header(slot_bytes, layout)
    if header_error(h, slot, layout=layout) is not None:
        return False, h
    hs = layout.header_size
    payload = slot_bytes[hs:hs + h.payload_size]
    return zlib.crc32(payload) == h.payload_crc32, h


def slot_status_word(slot_bytes: bytes, slot: str, layout: Layout = F407) -> str:
    """'ok:1.0.0' | 'bad' | 'empty' as the A/B BL prints it (bl.c print_slot): ok when the slot is
    valid, else 'empty' when the whole slot is erased (slot_is_empty), else 'bad'."""
    ok, h = slot_valid(slot_bytes, slot, layout)
    if ok:
        return f"ok:{h.version}"
    return "empty" if bytes(slot_bytes).count(0xFF) == len(slot_bytes) else "bad"


def naive_status_word(slot_bytes: bytes, layout: Layout = F407) -> str:
    """The naive BL's slotA word: it trusts the header magic alone (no CRC check)."""
    h = parse_header(slot_bytes, layout)
    if h is not None and h.magic == IMAGE_MAGIC:
        return f"ok:{h.version}"
    return "empty" if bytes(slot_bytes).count(0xFF) == len(slot_bytes) else "bad"


def naive_header_error(h: Header | None, layout: Layout = F407) -> str | None:
    """The naive BL's BEGIN check (bl_update.c, DESIGN_NAIVE): magic + header CRC, then size."""
    if h is None or h.magic != IMAGE_MAGIC or not h.crc_ok():
        return "BAD_HDR"
    if h.payload_size == 0 or h.payload_size > layout.max_payload or h.payload_size % layout.align:
        return "SIZE"
    return None


@dataclass
class Image:
    name: str                       # "v2_B.img"
    base: str                       # "v2"
    slot: str                       # "B"
    data: bytes
    header: Header | None
    path: Path | None = None
    layout: Layout = F407

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def payload(self) -> bytes:
        return self.data[self.layout.header_size:]

    @property
    def variant(self) -> str:
        for v in APP_VARIANTS:
            if self.base.endswith(v):
                return v
        return ""

    @property
    def version(self) -> str:
        """Full version as the app banner prints it: '2.0.0' or '2.0.0-crash'."""
        if self.header is None:
            return "?"
        return self.header.version + (f"-{self.variant}" if self.variant else "")


@dataclass
class ImageSet:
    dir: Path
    images: dict[str, Image] = field(default_factory=dict)       # by file name
    manifest: dict = field(default_factory=dict)
    golden: dict[str, Path] = field(default_factory=dict)        # design -> path
    layout: Layout = F407

    @classmethod
    def load(cls, d: Path, layout: Layout = F407) -> "ImageSet":
        s = cls(Path(d), layout=layout)
        if not s.dir.is_dir():
            return s
        for p in sorted(s.dir.glob("*.img")):
            m = NAME_RE.match(p.name)
            if not m:
                continue
            data = p.read_bytes()
            s.images[p.name] = Image(p.name, m["base"], m["slot"], data, parse_header(data, layout), p, layout)
        for design in GOLDEN_KEYS:
            g = s.dir / f"golden_{design}.bin"
            if g.is_file():
                s.golden[design] = g
        mf = s.dir / "manifest.json"
        if mf.is_file():
            try:
                s.manifest = json.loads(mf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                s.manifest = {"_error": "manifest.json is not valid JSON"}
        return s

    def get(self, base: str, slot: str) -> Image:
        img = self.images.get(f"{base}_{slot}.img")
        if img is None:
            from ._bench import BenchError
            raise BenchError("image_not_found", f"{base}_{slot}.img not found in {self.dir}.",
                             ["Build the boot images: firmware/boot (python firmware/boot/build.py)",
                              "Check [stress] images_dir in stress.toml"])
        return img

    def has(self, base: str, slot: str) -> bool:
        return f"{base}_{slot}.img" in self.images

    def for_slot(self, slot: str) -> list[Image]:
        return [i for i in self.images.values() if i.slot == slot]

    def version_of(self, base: str) -> str | None:
        for i in self.images.values():
            if i.base == base:
                return i.version
        return None

    @property
    def manifest_target(self) -> str:
        """The target the images were built for: firmware/boot_c0/mkimage.py writes "target": "c031"
        into every manifest entry (a top-level "target" is accepted too); none means f407 (the F407
        mkimage.py writes no target). Entries that disagree give "mixed"."""
        t = self.manifest.get("target")
        if isinstance(t, str):
            return t
        ts = {v.get("target", "f407") for v in self.manifest.values() if isinstance(v, dict)}
        if len(ts) > 1:
            return "mixed"
        return ts.pop() if ts else "f407"

    def target_problem(self) -> str | None:
        """The images were built for another board than the selected --target."""
        if not self.manifest or "_error" in self.manifest:
            return None
        if self.manifest_target != self.layout.target:
            return (f"{self.dir}/manifest.json says target={self.manifest_target}, "
                    f"but --target is {self.layout.target}")
        return None

    def manifest_problems(self) -> list[str]:
        """Compare manifest.json sha256 entries with the files on disk."""
        probs = []
        if "_error" in self.manifest:
            return [self.manifest["_error"]]
        for name, ent in self.manifest.items():
            if not isinstance(ent, dict):
                continue
            p = self.dir / name
            if not p.is_file():
                probs.append(f"{name}: in manifest but missing on disk")
                continue
            want = ent.get("sha256")
            if want and hashlib.sha256(p.read_bytes()).hexdigest() != want:
                probs.append(f"{name}: sha256 differs from manifest")
        return probs

    def manifest_digest(self) -> str | None:
        mf = self.dir / "manifest.json"
        return hashlib.sha256(mf.read_bytes()).hexdigest() if mf.is_file() else None


def slot_label(slot_bytes: bytes, candidates: list[Image]) -> str:
    """Match label from the expected-length hash (SPEC 2): first candidate whose hash over
    [0, len(image)) matches, else 'empty' (all 0xFF) or 'other'."""
    for img in candidates:
        if hashlib.sha256(slot_bytes[:len(img.data)]).hexdigest() == img.sha256:
            return img.base
    if slot_bytes.count(0xFF) == len(slot_bytes):
        return "empty"
    return "other"


def label_candidates(images: ImageSet, slot: str, first: list[Image] | None = None) -> list[Image]:
    """The trial's old/new image first, then v1/v2, then everything else longest first
    (so a full v2 is not mistaken for its own truncated prefix)."""
    out: list[Image] = []
    for img in (first or []):
        if img is not None and img.slot == slot and img not in out:
            out.append(img)
    for base in ("v1", "v2"):
        if images.has(base, slot) and images.get(base, slot) not in out:
            out.append(images.get(base, slot))
    rest = sorted((i for i in images.for_slot(slot) if i not in out), key=lambda i: -len(i.data))
    return out + rest


def whole_slot_sha(slot_bytes: bytes, layout: Layout = F407) -> str:
    assert len(slot_bytes) == layout.slot_size
    return hashlib.sha256(slot_bytes).hexdigest()
