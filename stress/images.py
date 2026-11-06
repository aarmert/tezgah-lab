"""Image header format (SPEC 2), the image set + manifest, and slot match labels."""
from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .layout import (BOARD_ID, HEADER_CRC_OFF, HEADER_SIZE, HEADER_VERSION, IMAGE_MAGIC, MAX_PAYLOAD,
                     SLOT_SIZE, payload_addr, version_str)

HDR_FMT = "<IHHIIIIII32s"          # 64 bytes
NAME_RE = re.compile(r"^(?P<base>[a-z0-9_]+?)_(?P<slot>[AB])\.img$")
APP_VARIANTS = ("crash", "hang", "noconfirm")


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

    @property
    def version(self) -> str:
        return version_str(self.fw_version)

    def crc_ok(self) -> bool:
        return zlib.crc32(self.raw[:HEADER_CRC_OFF]) == self.header_crc32


def build_header(*, fw_version: int, security_counter: int, link_addr: int, payload: bytes,
                 board_id: int = BOARD_ID, payload_size: int | None = None, flags: int = 0,
                 magic: int = IMAGE_MAGIC) -> bytes:
    size = len(payload) if payload_size is None else payload_size
    first = struct.pack(HDR_FMT, magic, HEADER_VERSION, flags, board_id, fw_version, security_counter,
                        link_addr, size, zlib.crc32(payload), hashlib.sha256(payload).digest())
    body = first + b"\xff" * (HEADER_CRC_OFF - len(first))
    return body + struct.pack("<I", zlib.crc32(body))


def parse_header(b: bytes) -> Header | None:
    if len(b) < HEADER_SIZE:
        return None
    f = struct.unpack_from(HDR_FMT, b, 0)
    (hcrc,) = struct.unpack_from("<I", b, HEADER_CRC_OFF)
    return Header(*f, header_crc32=hcrc, raw=bytes(b[:HEADER_SIZE]))


def header_error(h: Header | None, slot: str | None = None, *, check_link: bool = True) -> str | None:
    """The BL's BEGIN-time header check, in the BL's order. Returns an ERR code or None."""
    if h is None or h.magic != IMAGE_MAGIC or h.header_version != HEADER_VERSION or not h.crc_ok():
        return "BAD_HDR"
    if h.board_id != BOARD_ID:
        return "BOARD"
    if h.payload_size > MAX_PAYLOAD or h.payload_size % 4 or h.payload_size == 0:
        return "SIZE"
    if check_link and slot is not None and h.link_addr != payload_addr(slot):
        return "LINK"
    return None


def slot_valid(slot_bytes: bytes, slot: str) -> tuple[bool, Header | None]:
    """SPEC 2 validity: magic, header CRC, board, link_addr, size and payload CRC32."""
    h = parse_header(slot_bytes)
    if header_error(h, slot) is not None:
        return False, h
    payload = slot_bytes[HEADER_SIZE:HEADER_SIZE + h.payload_size]
    return zlib.crc32(payload) == h.payload_crc32, h


def slot_status_word(slot_bytes: bytes, slot: str) -> str:
    """'ok:1.0.0' | 'bad' | 'empty' as printed in the BL status line."""
    if all(b == 0xFF for b in slot_bytes[:HEADER_SIZE]):
        return "empty"
    ok, h = slot_valid(slot_bytes, slot)
    return f"ok:{h.version}" if ok else "bad"


@dataclass
class Image:
    name: str                       # "v2_B.img"
    base: str                       # "v2"
    slot: str                       # "B"
    data: bytes
    header: Header | None
    path: Path | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def payload(self) -> bytes:
        return self.data[HEADER_SIZE:]

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

    @classmethod
    def load(cls, d: Path) -> "ImageSet":
        s = cls(Path(d))
        if not s.dir.is_dir():
            return s
        for p in sorted(s.dir.glob("*.img")):
            m = NAME_RE.match(p.name)
            if not m:
                continue
            data = p.read_bytes()
            s.images[p.name] = Image(p.name, m["base"], m["slot"], data, parse_header(data), p)
        for design in ("ab", "naive"):
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


def whole_slot_sha(slot_bytes: bytes) -> str:
    assert len(slot_bytes) == SLOT_SIZE
    return hashlib.sha256(slot_bytes).hexdigest()
