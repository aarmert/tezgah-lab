"""NUCLEO-C031C6 (STM32C031C6T6) layout, SPEC-C031C6 sections 1, 2, 4. Mirrors firmware/boot_c0/layout.h.

16 pages of 2 KB: BL 0-5, META0 6, META1 7, SLOT A 8-11, SLOT B 12-15. Fault-point indices are page
numbers: a slot erase is one `ERASE <first page>` point (8 or 12), a rotation is `ROTATE 6|7`.
"""
from __future__ import annotations

from .layout import Layout

FLASH_BASE = 0x08000000
PAGE_SIZE = 0x800
PAGES = 16
GOLDEN_SIZE = PAGES * PAGE_SIZE          # golden_*.bin covers 0x08000000..0x08007FFF

PAGE = {n: (FLASH_BASE + n * PAGE_SIZE, PAGE_SIZE) for n in range(PAGES)}
BL_PAGES = tuple(range(0, 6))
META0_PAGE, META1_PAGE = 6, 7
SLOT_PAGES = {"A": tuple(range(8, 12)), "B": tuple(range(12, 16))}

SLOT_SIZE = 4 * PAGE_SIZE                # 8 KB
HEADER_SIZE = 256                        # 48-word vector table, VTOR alignment (SPEC-C031C6 2)
HEADER_CRC_OFF = 252
MAX_PAYLOAD = SLOT_SIZE - HEADER_SIZE
BOARD_ID = 0xC031C600
ALIGN = 8                                # double-word programming
CHUNK = 192                              # 24 double-words
LINE_MAX_CHARS = 400                     # one limit for every line (BEGIN = 10 + 344)
APP_LINE_MAX = 96
RAM_TOP = 0x20003000                     # 12 KB SRAM
UID_ADDR = 0x1FFF7550                    # (verify, RM0490 "Unique device ID")
BEGIN_TIMEOUT_S = 1.0                    # 4 pages ~ 90 ms

LAYOUT = Layout(
    target="c031", flash_base=FLASH_BASE, golden_size=GOLDEN_SIZE, units=PAGE, bl_units=BL_PAGES,
    slot_units=SLOT_PAGES, meta_units=(META0_PAGE, META1_PAGE), header_size=HEADER_SIZE,
    header_crc_off=HEADER_CRC_OFF, board_id=BOARD_ID, align=ALIGN, program_unit=8, chunk=CHUNK,
    line_max=LINE_MAX_CHARS, begin_max=LINE_MAX_CHARS, app_line_max=APP_LINE_MAX, ram_top=RAM_TOP,
    uid_addr=UID_ADDR, begin_timeout_s=BEGIN_TIMEOUT_S, images_dir="firmware/boot_c0/build/images",
    data_err_len="SEQ", hardfault_regs=False)

assert LAYOUT.slot_size == SLOT_SIZE and LAYOUT.max_payload == 8192 - 256
assert LAYOUT.header_b64_len == 344
