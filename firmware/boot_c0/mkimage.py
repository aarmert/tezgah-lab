#!/usr/bin/env python3
"""
Makes the NUCLEO-C031C6 update images, the golden flash images and manifest.json
(docs/stress/SPEC.md section 2 with the docs/stress/SPEC-C031C6.md section 2 overrides: 256 B
header, header CRC at 252, payload a multiple of 8, board_id 0xC031C600, 32 KB goldens).

    python firmware/boot_c0/mkimage.py                   # all images from firmware/boot_c0/build/
    python firmware/boot_c0/mkimage.py --payload app.bin --version 1.0.0 --sc 1 --slot A -o x.img

Addresses come from layout.h (parsed here, so there is a single source of truth).
wrong_board_{A,B}.img is a real F407 v2 app (firmware/boot/build/app_v2_{A,B}.bin, linked for the
F407 slot) with the F407 board id and link address, in the C0 header format.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
F407_BUILD = HERE.parent / "boot" / "build"
TARGET = "c031"


def _parse_layout(path: Path = HERE / "layout.h") -> dict[str, int]:
    vals: dict[str, int] = {}
    for m in re.finditer(r"^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)u?\b", path.read_text(), re.M):
        vals[m.group(1)] = int(m.group(2), 0)
    return vals


L = _parse_layout()
BL_BASE = L["BL_BASE"]
BL_SIZE = L["BL_SIZE"]
META0_BASE = L["META0_BASE"]
META1_BASE = L["META1_BASE"]
SLOT_BASE = {"A": L["SLOT_A_BASE"], "B": L["SLOT_B_BASE"]}
SLOT_SIZE = L["SLOT_SIZE"]
HDR_SIZE = L["HDR_SIZE"]
PAYLOAD_MAX = SLOT_SIZE - HDR_SIZE
PROG_UNIT = L["PROG_UNIT"]
PAGE_SIZE = L["PAGE_SIZE"]
META0_SIZE = L["META0_SIZE"]
IMG_MAGIC = L["IMG_MAGIC"]
IMG_HDR_VERSION = L["IMG_HDR_VERSION"]
BOARD_ID = L["BOARD_ID"]
META_MAGIC = L["META_MAGIC"]
GOLDEN_BASE = BL_BASE
GOLDEN_SIZE = L["FLASH_SIZE"]              # 0x08000000..0x08007FFF
HDR_CRC_OFF = L["HDR_CRC_OFF"]             # 252

META_KIND = {"CONFIRMED": 1, "TRIAL": 2, "ATTEMPT": 3, "CONFIRM": 4, "ROLLBACK": 5, "REPAIR": 6}

# App variants: name -> version, security_counter, banner suffix, C define
APPS: dict[str, dict] = {
    "v1":          {"version": "1.0.0", "sc": 1, "suffix": "",           "define": None},
    "v2":          {"version": "2.0.0", "sc": 1, "suffix": "",           "define": None},
    "v2crash":     {"version": "2.0.0", "sc": 1, "suffix": "-crash",     "define": "APP_VARIANT_CRASH"},
    "v2hang":      {"version": "2.0.0", "sc": 1, "suffix": "-hang",      "define": "APP_VARIANT_HANG"},
    "v2noconfirm": {"version": "2.0.0", "sc": 1, "suffix": "-noconfirm", "define": "APP_VARIANT_NOCONFIRM"},
    "old":         {"version": "0.9.0", "sc": 0, "suffix": "",           "define": None},
}
SLOTS = ("A", "B")
# Bad images: name -> the BL's expected reply code (SPEC section 8, test 6)
BAD = {"bad_crc": "IMG_CRC", "wrong_board": "BOARD", "truncated": "SHORT", "oversize": "SIZE",
       "old": "VERSION"}
OVERSIZE_PAYLOAD = 9000                    # SPEC-C031C6 2 (> 8192 - 256, a multiple of 8)
# wrong_board: the real F407 v2 image (SPEC-C031C6 2), F407 board id and F407 link address
F407_BOARD_ID = 0xF407D15C
F407_PAYLOAD_ADDR = {"A": 0x08008200, "B": 0x0800C200}
# golden file -> (BL build, app build suffix of the v1 in slot A). The *_nohooks goldens are a
# release board: release BL and release (no FAULT_HOOKS) app.
GOLDEN = {"golden_ab": ("bl_ab_hooks", ""), "golden_naive": ("bl_naive_hooks", ""),
          "golden_ab_nohooks": ("bl_ab", "_nohooks"), "golden_naive_nohooks": ("bl_naive", "_nohooks"),
          "golden_ab_metafull": ("bl_ab_hooks", "")}
# images/release/: the whole image set from the release apps, with release goldens under the usual
# names, so a stress run on a release board only needs images_dir = .../images/release.
RELEASE_DIR = "release"
GOLDEN_RELEASE = {"golden_ab": ("bl_ab", "_nohooks"), "golden_naive": ("bl_naive", "_nohooks")}
# golden_ab_metafull: META0 (64 records) holds this many, so the first append after the next
# update's TRIAL record rotates into META1 (SPEC 2 and 8, --meta-near-full).
META0_RECORDS = META0_SIZE // 32
META_FULL_RECORDS = META0_RECORDS - 1


def payload_addr(slot: str) -> int:
    return SLOT_BASE[slot] + HDR_SIZE


def version_word(v: str) -> int:
    a, b, c = (int(x) for x in v.split("."))
    return (a << 16) | (b << 8) | c


def make_header(payload: bytes, version: str, sc: int, link_addr: int, *,
                board_id: int = BOARD_ID, payload_size: int | None = None) -> bytes:
    size = len(payload) if payload_size is None else payload_size
    h = struct.pack("<IHHIIIIII", IMG_MAGIC, IMG_HDR_VERSION, 0, board_id, version_word(version),
                    sc, link_addr, size, zlib.crc32(payload) & 0xFFFFFFFF)
    h += hashlib.sha256(payload).digest()
    assert len(h) == 64
    h += b"\xff" * (HDR_CRC_OFF - len(h))
    return h + struct.pack("<I", zlib.crc32(h) & 0xFFFFFFFF)


def parse_header(h: bytes) -> dict:
    (magic, hv, flags, board, ver, sc, link, size, pcrc) = struct.unpack_from("<IHHIIIIII", h, 0)
    return {"magic": magic, "header_version": hv, "flags": flags, "board_id": board,
            "fw_version": ver, "security_counter": sc, "link_addr": link, "payload_size": size,
            "payload_crc32": pcrc, "payload_sha256": h[32:64],
            "header_crc32": struct.unpack_from("<I", h, HDR_CRC_OFF)[0]}


def meta_record(seq: int, kind: int, active: int, trial: int, attempts: int, min_sc: int,
                erase_self: int, erase_other: int) -> bytes:
    r = struct.pack("<IIBBBBIIII", META_MAGIC, seq, kind, active, trial, attempts, min_sc,
                    erase_self, erase_other, 0xFFFFFFFF)
    return r + struct.pack("<I", zlib.crc32(r) & 0xFFFFFFFF)


def pad8(b: bytes) -> bytes:
    """Payload padding to the 64-bit programming unit (SPEC-C031C6 2)."""
    return b + b"\xff" * (-len(b) % PROG_UNIT)


def ihex(data: bytes, base: int) -> str:
    """Intel HEX with every byte (erased areas included, so a programmer erases those sectors)."""
    out, upper = [], None

    def rec(addr16: int, typ: int, payload: bytes) -> str:
        r = bytes([len(payload), addr16 >> 8, addr16 & 0xFF, typ]) + payload
        return ":" + r.hex().upper() + f"{(-sum(r)) & 0xFF:02X}"

    for off in range(0, len(data), 16):
        addr = base + off
        if addr >> 16 != upper:
            upper = addr >> 16
            out.append(rec(0, 4, struct.pack(">H", upper)))
        out.append(rec(addr & 0xFFFF, 0, data[off:off + 16]))
    out.append(":00000001FF")
    return "\n".join(out) + "\n"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def f407_payload(f407_build: Path, slot: str) -> bytes:
    p = f407_build / f"app_v2_{slot}.bin"
    if not p.is_file():
        raise FileNotFoundError(f"{p} missing: build the F407 firmware first "
                                "(python firmware/boot/build_boot.py --json)")
    return pad8(p.read_bytes())


def build_all(build: Path = BUILD, f407_build: Path = F407_BUILD) -> dict:
    """Reads build/app_<variant>_<slot>[_nohooks].bin, build/bl_*.bin and the F407 v2 apps and
    writes build/images/ (hooks apps) and build/images/release/ (release apps). Returns the
    images/ manifest."""
    m = build_set(build, build / "images", "", GOLDEN, f407_build)
    build_set(build, build / "images" / RELEASE_DIR, "_nohooks", GOLDEN_RELEASE, f407_build)
    return m


def build_set(build: Path, out: Path, app_suffix: str, goldens: dict, f407_build: Path = F407_BUILD) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}

    def emit(name: str, data: bytes, **info) -> None:
        (out / name).write_bytes(data)
        manifest[name] = {"sha256": sha(data), "size": len(data), "target": TARGET, **info}

    def payload(var: str, slot: str, suffix: str) -> bytes:
        p = pad8((build / f"app_{var}_{slot}{suffix}.bin").read_bytes())
        if len(p) > PAYLOAD_MAX:
            raise ValueError(f"app_{var}_{slot}{suffix}: payload {len(p)} > {PAYLOAD_MAX}")
        return p

    payloads: dict[tuple[str, str], bytes] = {}
    for var, meta in APPS.items():
        for slot in SLOTS:
            p = payloads[(var, slot)] = payload(var, slot, app_suffix)
            hdr = make_header(p, meta["version"], meta["sc"], payload_addr(slot))
            emit(f"{var}_{slot}.img", hdr + p, payload_sha256=sha(p), version=meta["version"],
                 slot=slot, variant=var, security_counter=meta["sc"], payload_size=len(p),
                 link_addr=f"0x{payload_addr(slot):08X}",
                 expected_err=BAD.get(var), hooks=not app_suffix)

    for slot in SLOTS:
        v2 = payloads[("v2", slot)]
        link = payload_addr(slot)
        common = {"version": "2.0.0", "slot": slot, "security_counter": 1,
                  "link_addr": f"0x{link:08X}"}
        # bad_crc: header made from the good payload, then one payload byte flipped
        bad = bytearray(v2)
        bad[len(bad) // 2] ^= 0x01
        emit(f"bad_crc_{slot}.img", make_header(v2, "2.0.0", 1, link) + bytes(bad),
             payload_sha256=sha(bytes(bad)), payload_size=len(v2), variant="bad_crc",
             expected_err=BAD["bad_crc"], **common)
        # wrong_board: the real F407 v2 app (Cortex-M4 code linked for the F407 slot), its F407
        # board id and link address, in the C0 header format so the BL gets as far as the board
        # check (valid header CRC). Expected: ERR BOARD.
        f4 = f407_payload(f407_build, slot)
        f4link = F407_PAYLOAD_ADDR[slot]
        emit(f"wrong_board_{slot}.img",
             make_header(f4, "2.0.0", 1, f4link, board_id=F407_BOARD_ID) + f4,
             payload_sha256=sha(f4), payload_size=len(f4), variant="wrong_board",
             expected_err=BAD["wrong_board"], board_id=f"0x{F407_BOARD_ID:08X}",
             source=f"firmware/boot/build/app_v2_{slot}.bin (F407)",
             **{**common, "link_addr": f"0x{f4link:08X}"})
        # truncated: full v2 header, only the first half of the payload
        half = v2[: (len(v2) // 2) & ~(PROG_UNIT - 1)]
        emit(f"truncated_{slot}.img", make_header(v2, "2.0.0", 1, link) + half,
             payload_sha256=sha(half), payload_size=len(v2), variant="truncated",
             expected_err=BAD["truncated"], **common)
        # oversize: payload_size = 9000 (> 8192 - 256), payload padded to that size
        big = v2 + b"\xff" * (OVERSIZE_PAYLOAD - len(v2))
        emit(f"oversize_{slot}.img", make_header(big, "2.0.0", 1, link) + big,
             payload_sha256=sha(big), payload_size=OVERSIZE_PAYLOAD, variant="oversize",
             expected_err=BAD["oversize"], **common)

    # Golden flash images: BL, META0 with the initial record(s), v1 in A, B and META1 erased
    first = meta_record(seq=1, kind=META_KIND["CONFIRMED"], active=0, trial=0xFF, attempts=0,
                        min_sc=1, erase_self=0, erase_other=0)
    full = first + b"".join(meta_record(seq=n, kind=META_KIND["CONFIRM"], active=0, trial=0xFF, attempts=0,
                                        min_sc=1, erase_self=0, erase_other=0)
                            for n in range(2, META_FULL_RECORDS + 1))
    for gname, (blname, suffix) in goldens.items():
        rec = full if gname.endswith("_metafull") else first
        p = payload("v1", "A", suffix)
        v1a = make_header(p, APPS["v1"]["version"], APPS["v1"]["sc"], payload_addr("A")) + p
        bl = (build / f"{blname}.bin").read_bytes()
        if len(bl) > BL_SIZE:
            raise ValueError(f"{blname}: {len(bl)} > {BL_SIZE}")
        img = bytearray(b"\xff" * GOLDEN_SIZE)
        img[0:len(bl)] = bl
        m0 = META0_BASE - GOLDEN_BASE
        img[m0:m0 + len(rec)] = rec
        a = SLOT_BASE["A"] - GOLDEN_BASE
        img[a:a + len(v1a)] = v1a
        data = bytes(img)
        design = "naive" if "naive" in gname else "ab"
        info = {"design": design, "bl": blname, "bl_sha256": sha(bl), "version": "1.0.0",
                "slot": "A", "payload_sha256": sha(p), "app": f"app_v1_A{suffix}",
                "base": f"0x{GOLDEN_BASE:08X}", "meta0_records": len(rec) // 32}
        emit(f"{gname}.bin", data, **info)
        emit(f"{gname}.hex", ihex(data, GOLDEN_BASE).encode(), **info)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", type=Path, default=BUILD)
    ap.add_argument("--payload", type=Path, help="single image mode: payload .bin")
    ap.add_argument("--version")
    ap.add_argument("--sc", type=int, default=1)
    ap.add_argument("--slot", choices=SLOTS)
    ap.add_argument("-o", "--output", type=Path)
    a = ap.parse_args()
    if a.payload:
        if not (a.version and a.slot and a.output):
            ap.error("--payload needs --version, --slot and -o")
        p = pad8(a.payload.read_bytes())
        a.output.write_bytes(make_header(p, a.version, a.sc, payload_addr(a.slot)) + p)
        print(a.output)
        return 0
    m = build_all(a.build_dir)
    print(f"{len(m)} files -> {a.build_dir / 'images'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
