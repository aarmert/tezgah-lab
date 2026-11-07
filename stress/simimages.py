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
from .layout import F407, KIND_NUM, META_RECORD, TRIAL_NONE, Layout, version_int
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


def _key(base: str, slot: str, layout: Layout) -> str:
    # F407 keys are unchanged from before the C031 target existed (same bytes, same manifests)
    return f"{base}:{slot}" if layout.target == "f407" else f"{layout.target}:{base}:{slot}"


def make_payload(base: str, slot: str, layout: Layout = F407) -> bytes:
    version, _, size, variant = APPS[base]
    key = _key(base, slot, layout)
    p = bytearray(prng(key + ":fill", size))
    for name, a, b in REGIONS:
        p[a:b] = prng(f"{key}:{name}", b - a)
    p[0:8] = struct.pack("<II", layout.ram_top, layout.payload_addr(slot) + 0x201)
    text = f"BOOT app {version}{'-' + variant if variant else ''}".encode()
    p[0x800:0x800 + len(text)] = text
    return bytes(p)


def make_image(base: str, slot: str, layout: Layout = F407) -> bytes:
    version, sc, _, _ = APPS[base]
    payload = make_payload(base, slot, layout)
    return build_header(fw_version=version_int(version), security_counter=sc,
                        link_addr=layout.payload_addr(slot), payload=payload, layout=layout) + payload


# F407 board id: the C031's wrong_board image carries a real F407 board id (SPEC-C031C6 2)
F407_BOARD_ID = F407.board_id
OVERSIZE = {"f407": 20000, "c031": 9000}


def make_bad_image(kind: str, slot: str, layout: Layout = F407) -> bytes:
    v2 = make_payload("v2", slot, layout)
    link = layout.payload_addr(slot)
    ver = version_int("2.0.0")
    kw = dict(fw_version=ver, security_counter=1, link_addr=link, layout=layout)
    if kind == "bad_crc":
        p = bytearray(v2)
        p[0x1500] ^= 0x01
        return build_header(payload=v2, **kw) + bytes(p)
    if kind == "wrong_board":
        if layout.target == "f407":
            return build_header(payload=v2, board_id=0xC0C0C0C0, **kw) + v2
        # C031 (firmware/boot_c0/mkimage.py): the real F407 v2 app, with the F407 board id and link
        # address, in the C0 header format (header_crc at 252), so BEGIN answers BOARD. A literal
        # F407 .img would answer BAD_HDR: its header_crc is at 508.
        f4 = make_payload("v2", slot, F407)
        return build_header(fw_version=ver, security_counter=1, link_addr=F407.payload_addr(slot),
                            payload=f4, board_id=F407_BOARD_ID, layout=layout) + f4
    if kind == "truncated":
        return build_header(payload=v2, **kw) + v2[:len(v2) // 2]
    if kind == "oversize":
        n = OVERSIZE.get(layout.target, 20000)
        p = v2 + prng(f"oversize:{slot}", n - len(v2))
        return build_header(payload=p, **kw) + p
    raise ValueError(kind)


BAD_KINDS = ("bad_crc", "wrong_board", "truncated", "oversize")


def meta_full_records(layout: Layout = F407) -> int:
    """mkimage.py META_FULL_RECORDS: one record short of a full META0 (F407 511, C031 63)."""
    return layout.records_per_meta() - 1


META_FULL_RECORDS = meta_full_records(F407)


def make_golden(design: str, meta_records: int = 1, layout: Layout = F407) -> bytes:
    """design ab / naive; ab with meta_records = meta_full_records() is golden_ab_metafull."""
    g = bytearray(b"\xff" * layout.golden_size)
    bl = BL_MARK[design] + prng(f"bl:{design}", min(0x3000, layout.bl_size - len(BL_MARK[design])))
    g[0:len(bl)] = bl
    if design == "ab":
        o = layout.meta_region(0)[0] - layout.flash_base
        for n in range(1, meta_records + 1):
            kind = KIND_NUM["CONFIRMED"] if n == 1 else KIND_NUM["CONFIRM"]
            g[o + (n - 1) * META_RECORD:o + n * META_RECORD] = pack_record(n, kind, 0, TRIAL_NONE, 0, 1, 0, 0)
    v1 = make_image("v1", "A", layout)
    o = layout.slot_base["A"] - layout.flash_base
    g[o:o + len(v1)] = v1
    return bytes(g)


def write_image_set(out: Path, layout: Layout = F407) -> Path:
    """Write every image of SPEC 2 plus golden_*.bin and manifest.json. Idempotent."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {}
    for slot in ("A", "B"):
        for base in APPS:
            files[f"{base}_{slot}.img"] = make_image(base, slot, layout)
        for kind in BAD_KINDS:
            files[f"{kind}_{slot}.img"] = make_bad_image(kind, slot, layout)
    for design in ("ab", "naive"):
        files[f"golden_{design}.bin"] = make_golden(design, layout=layout)
    files["golden_ab_metafull.bin"] = make_golden("ab", meta_full_records(layout), layout)
    manifest = {}
    for name, data in files.items():
        p = out / name
        if not p.is_file() or p.read_bytes() != data:
            p.write_bytes(data)
        ent = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if name.endswith(".img"):
            base, slot = name[:-4].rsplit("_", 1)
            ent.update(payload_sha256=hashlib.sha256(data[layout.header_size:]).hexdigest(), slot=slot,
                       version=APPS.get(base, ("2.0.0",))[0] if base in APPS else "2.0.0")
        if layout.target != "f407":
            ent["target"] = layout.target          # like firmware/boot_c0/mkimage.py
        manifest[name] = ent
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out


def app_registry(layout: Layout = F407) -> dict[tuple[str, str, bytes], AppInfo]:
    """(slot, region, region bytes) -> AppInfo for every known app image."""
    reg = {}
    for base, (version, sc, _, variant) in APPS.items():
        info = AppInfo(base, version + (f"-{variant}" if variant else ""), variant, sc)
        for slot in ("A", "B"):
            p = make_payload(base, slot, layout)
            for name, a, b in REGIONS:
                reg[(slot, name, p[a:b])] = info
    return reg


assert F407_BOARD_ID == 0xF407D15C
