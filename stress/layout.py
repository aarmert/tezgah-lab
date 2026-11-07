"""Flash layout and binary constants (SPEC sections 1, 2, 4.1). Mirrors firmware/boot/layout.h."""

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
