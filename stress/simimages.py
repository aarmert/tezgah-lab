"""Synthetic image set for --sim and the unit tests (SPEC 2 header format, mkimage.py names).

Payloads are deterministic pseudo-random bytes with a fixed region layout. The simulator decides
what an app does by checking which known image each region of the slot matches:
vec (vector table) -> rt (runtime/startup) -> banner -> main loop -> confirm -> hf (HardFault handler).
A region that matches no known image is "garbage"; executing it crashes or locks the core.
"""
from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

from .images import build_header
from .layout import (BOARD_ID, GOLDEN_SIZE, HEADER_SIZE, KIND_NUM, META_RECORD, RAM_TOP, SECTORS,
                     TRIAL_NONE, payload_addr, version_int)
from .meta import pack_record

REGIONS = (("vec", 0x000, 0x188), ("rt", 0x200, 0x800), ("banner", 0x800, 0xA00),
           ("main", 0xA00, 0x1000), ("confirm", 0x1000, 0x1200), ("hf", 0x1200, 0x1300))
BL_MARK = {"ab": b"SIMBL design=ab\n", "naive": b"SIMBL design=naive\n"}

# base -> (version, security counter, payload size, variant)
APPS = {
    "v1": ("1.0.0", 1, 0x1800, ""),
    "v2": ("2.0.0", 1, 0x1A00, ""),
    "v2crash": ("2.0.0", 1, 0x1A00, "crash"),
    "v2hang": ("2.0.0", 1, 0x1A00, "hang"),
    "v2noconfirm": ("2.0.0", 1, 0x1A00, "noconfirm"),
    "old": ("0.9.0", 0, 0x1600, ""),
}


@dataclass(frozen=True)
class AppInfo:
    base: str
    version: str        # banner version incl. suffix, e.g. "2.0.0-crash"
    variant: str
    security_counter: int


def prng(key: str, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"{key}:{i}".encode()).digest()
        i += 1
    return bytes(out[:n])


def make_payload(base: str, slot: str) -> bytes:
    version, _, size, variant = APPS[base]
    key = f"{base}:{slot}"
    p = bytearray(prng(key + ":fill", size))
    for name, a, b in REGIONS:
        p[a:b] = prng(f"{key}:{name}", b - a)
    p[0:8] = struct.pack("<II", RAM_TOP, payload_addr(slot) + 0x201)
    text = f"BOOT app {version}{'-' + variant if variant else ''}".encode()
    p[0x800:0x800 + len(text)] = text
    return bytes(p)


def make_image(base: str, slot: str) -> bytes:
    version, sc, _, _ = APPS[base]
    payload = make_payload(base, slot)
    return build_header(fw_version=version_int(version), security_counter=sc,
                        link_addr=payload_addr(slot), payload=payload) + payload


def make_bad_image(kind: str, slot: str) -> bytes:
    v2 = make_payload("v2", slot)
    link = payload_addr(slot)
    ver = version_int("2.0.0")
    if kind == "bad_crc":
        p = bytearray(v2)
        p[0x1500] ^= 0x01
        return build_header(fw_version=ver, security_counter=1, link_addr=link, payload=v2) + bytes(p)
    if kind == "wrong_board":
        return build_header(fw_version=ver, security_counter=1, link_addr=link, payload=v2,
                            board_id=0xC0C0C0C0) + v2
    if kind == "truncated":
        return build_header(fw_version=ver, security_counter=1, link_addr=link, payload=v2) + v2[:len(v2) // 2]
    if kind == "oversize":
        p = v2 + prng(f"oversize:{slot}", 20000 - len(v2))
        return build_header(fw_version=ver, security_counter=1, link_addr=link, payload=p) + p
    raise ValueError(kind)


BAD_KINDS = ("bad_crc", "wrong_board", "truncated", "oversize")


META_FULL_RECORDS = SECTORS[1][1] // META_RECORD - 1     # mkimage.py META_FULL_RECORDS (511)


def make_golden(design: str, meta_records: int = 1) -> bytes:
    """design ab / naive; ab with meta_records = META_FULL_RECORDS is golden_ab_metafull."""
    g = bytearray(b"\xff" * GOLDEN_SIZE)
    bl = BL_MARK[design] + prng(f"bl:{design}", 0x3000)
    g[0:len(bl)] = bl
    if design == "ab":
        o = SECTORS[1][0] - SECTORS[0][0]
        for n in range(1, meta_records + 1):
            kind = KIND_NUM["CONFIRMED"] if n == 1 else KIND_NUM["CONFIRM"]
            g[o + (n - 1) * META_RECORD:o + n * META_RECORD] = pack_record(n, kind, 0, TRIAL_NONE, 0, 1, 0, 0)
    v1 = make_image("v1", "A")
    o = SECTORS[2][0] - SECTORS[0][0]
    g[o:o + len(v1)] = v1
    return bytes(g)


def write_image_set(out: Path) -> Path:
    """Write every image of SPEC 2 plus golden_*.bin and manifest.json. Idempotent."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {}
    for slot in ("A", "B"):
        for base in APPS:
            files[f"{base}_{slot}.img"] = make_image(base, slot)
        for kind in BAD_KINDS:
            files[f"{kind}_{slot}.img"] = make_bad_image(kind, slot)
    for design in ("ab", "naive"):
        files[f"golden_{design}.bin"] = make_golden(design)
    files["golden_ab_metafull.bin"] = make_golden("ab", META_FULL_RECORDS)
    manifest = {}
    for name, data in files.items():
        p = out / name
        if not p.is_file() or p.read_bytes() != data:
            p.write_bytes(data)
        ent = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if name.endswith(".img"):
            base, slot = name[:-4].rsplit("_", 1)
            ent.update(payload_sha256=hashlib.sha256(data[HEADER_SIZE:]).hexdigest(), slot=slot,
                       version=APPS.get(base, ("2.0.0",))[0] if base in APPS else "2.0.0")
        manifest[name] = ent
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out


def app_registry() -> dict[tuple[str, str, bytes], AppInfo]:
    """(slot, region, region bytes) -> AppInfo for every known app image."""
    reg = {}
    for base, (version, sc, _, variant) in APPS.items():
        info = AppInfo(base, version + (f"-{variant}" if variant else ""), variant, sc)
        for slot in ("A", "B"):
            p = make_payload(base, slot)
            for name, a, b in REGIONS:
                reg[(slot, name, p[a:b])] = info
    return reg


assert BOARD_ID == 0xF407D15C
