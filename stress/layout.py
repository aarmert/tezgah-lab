"""Flash layout and binary constants (SPEC sections 1, 2, 4.1). Mirrors firmware/boot/layout.h.

Two targets share one host tool (SPEC-C031C6 10): `F407` (this module's constants, the default) and
`C031` (stress/layout_c031.py). Code that must work on both takes a `Layout` (`get(target)`); the
module-level constants below are the F407 values and stay for the F407-only callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FLASH_BASE = 0x08000000
GOLDEN_SIZE = 0x20000            # golden_*.bin covers 0x08000000..0x0801FFFF

# sector number -> (base address, size)
SECTORS = {
    0: (0x08000000, 0x4000),     # bootloader
    1: (0x08004000, 0x4000),     # META0
    2: (0x08008000, 0x4000),     # SLOT A
    3: (0x0800C000, 0x4000),     # SLOT B
    4: (0x08010000, 0x10000),    # META1
}
BL_SECTOR, META0_SECTOR, SLOT_A_SECTOR, SLOT_B_SECTOR, META1_SECTOR = 0, 1, 2, 3, 4

SLOT_SIZE = 0x4000
SLOT_BASE = {"A": SECTORS[2][0], "B": SECTORS[3][0]}
SLOT_SECTOR = {"A": 2, "B": 3}
SLOT_NUM = {"A": 0, "B": 1}
SLOT_NAME = {0: "A", 1: "B"}
META_SECTORS = (1, 4)

HEADER_SIZE = 512
MAX_PAYLOAD = SLOT_SIZE - HEADER_SIZE
CHUNK = 192                      # max decoded bytes per UPD DATA
LINE_MAX_CHARS = 300             # BL: every update-mode line except UPD BEGIN (SPEC 4)
BEGIN_MAX_CHARS = 700            # BL line buffer; "UPD BEGIN " + 684 base64 chars = 694
APP_LINE_MAX = 96                # app command line buffer (app_rt.c)

IMAGE_MAGIC = 0x4D495A54         # "TZIM"
HEADER_VERSION = 1
BOARD_ID = 0xF407D15C
HEADER_CRC_OFF = 508

META_MAGIC = 0x4154454D          # "META"
META_RECORD = 32
KIND = {1: "CONFIRMED", 2: "TRIAL", 3: "ATTEMPT", 4: "CONFIRM", 5: "ROLLBACK", 6: "REPAIR"}
KIND_NUM = {v: k for k, v in KIND.items()}
TRIAL_NONE = 0xFF

RAM_BASE = 0x20000000
RAM_TOP = 0x20020000             # 128 KB SRAM
UID_ADDR = 0x1FFF7A10

PHASES = ("ERASE", "DATA", "HDR", "META", "ROTATE", "BOOT", "JUMP", "CONFIRM")
TARGETS = ("f407", "c031")


@dataclass(frozen=True)
class Layout:
    """Everything the host and the sim need to know about one target's flash and protocol.
    `units`: erase unit number (F407 sector, C031 page) -> (base, size). Fault-point indices use
    these numbers: ERASE <unit> for every unit of a slot erase, ROTATE <meta unit>."""
    target: str
    flash_base: int
    golden_size: int
    units: dict
    bl_units: tuple
    slot_units: dict                 # "A"/"B" -> tuple of erase units (ascending)
    meta_units: tuple                # (META0 unit, META1 unit)
    header_size: int
    header_crc_off: int
    board_id: int
    align: int                       # payload_size / DATA off / chunk length alignment
    program_unit: int                # bytes per flash program operation (word / double-word)
    chunk: int
    line_max: int                    # longest update-mode line (except BEGIN on the F407)
    begin_max: int                   # longest UPD BEGIN line
    app_line_max: int
    ram_top: int
    uid_addr: int
    begin_timeout_s: float           # host wait for the BEGIN reply (the slot erase)
    images_dir: str                  # default image folder (repo-relative)
    data_err_len: str = "SIZE"       # DATA reply when the decoded length is not aligned (C0: SEQ)
    hardfault_regs: bool = True      # CFSR/HFSR exist (M0+: printed as zeros)
    notes: dict = field(default_factory=dict)

    # --- derived -----------------------------------------------------------------------
    @property
    def slot_base(self) -> dict:
        return {s: self.units[u[0]][0] for s, u in self.slot_units.items()}

    @property
    def slot_size(self) -> int:
        return sum(self.units[u][1] for u in self.slot_units["A"])

    @property
    def max_payload(self) -> int:
        return self.slot_size - self.header_size

    @property
    def meta_size(self) -> int:
        return self.units[self.meta_units[0]][1]

    def meta_region(self, n: int) -> tuple[int, int]:
        return self.units[self.meta_units[n]]

    @property
    def bl_size(self) -> int:
        return sum(self.units[u][1] for u in self.bl_units)

    @property
    def header_b64_len(self) -> int:
        return 4 * ((self.header_size + 2) // 3)

    @property
    def flash_size(self) -> int:
        return self.golden_size

    def payload_addr(self, slot: str) -> int:
        return self.slot_base[slot] + self.header_size

    def other_meta(self, unit: int) -> int:
        a, b = self.meta_units
        return b if unit == a else a

    def is_meta1(self, unit: int) -> bool:
        return unit == self.meta_units[1]

    def records_per_meta(self, unit: int | None = None) -> int:
        return self.units[unit if unit is not None else self.meta_units[0]][1] // META_RECORD

    def looks_like_sp(self, word: int) -> bool:
        return RAM_BASE < word <= self.ram_top

    def unit_of(self, addr: int) -> int | None:
        for u, (base, size) in self.units.items():
            if base <= addr < base + size:
                return u
        return None


F407 = Layout(
    target="f407", flash_base=FLASH_BASE, golden_size=GOLDEN_SIZE, units=dict(SECTORS), bl_units=(0,),
    slot_units={"A": (2,), "B": (3,)}, meta_units=META_SECTORS, header_size=HEADER_SIZE,
    header_crc_off=HEADER_CRC_OFF, board_id=BOARD_ID, align=4, program_unit=4, chunk=CHUNK,
    line_max=LINE_MAX_CHARS, begin_max=BEGIN_MAX_CHARS, app_line_max=APP_LINE_MAX, ram_top=RAM_TOP,
    uid_addr=UID_ADDR, begin_timeout_s=3.0, images_dir="firmware/boot/build/images")


def get(target: str | None) -> Layout:
    t = (target or "f407").lower()
    if t == "f407":
        return F407
    if t == "c031":
        from .layout_c031 import LAYOUT
        return LAYOUT
    from ._bench import BenchError
    raise BenchError("bad_argument", f"Unknown target '{target}'.", [f"Valid targets: {', '.join(TARGETS)}"])


def payload_addr(slot: str) -> int:
    return SLOT_BASE[slot] + HEADER_SIZE


def other_slot(slot: str) -> str:
    return "B" if slot == "A" else "A"


def other_meta(sector: int) -> int:
    return 4 if sector == 1 else 1


def version_str(v: int) -> str:
    return f"{(v >> 16) & 0xFFFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


def version_int(s: str) -> int:
    a, b, c = (int(x) for x in s.split("."))
    return (a << 16) | (b << 8) | c


def looks_like_sp(word: int) -> bool:
    """Naive BL boot check (bl.c naive_bootable): RAM_BASE < word 0 of the payload <= RAM top."""
    return RAM_BASE < word <= RAM_TOP
