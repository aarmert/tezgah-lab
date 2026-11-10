/*
 * Flash/RAM layout of the bootloader stress test on the NUCLEO-C031C6 (public/stress/SPEC-C031C6.md
 * section 1). Mirrored in stress/layout_c031.py; mkimage.py parses this file. Keep them in sync.
 */
#ifndef LAYOUT_H
#define LAYOUT_H

/* STM32C031C6: 32 KB flash, 16 pages of 2 KB */
#define FLASH_BASE       0x08000000u
#define FLASH_SIZE       0x8000u
#define PAGE_SIZE        0x800u
#define BL_BASE          0x08000000u   /* pages 0-5, 12 KB */
#define BL_SIZE          0x3000u
#define META0_BASE       0x08003000u   /* page 6, 2 KB */
#define META0_SIZE       0x800u
#define META0_PAGE       6u
#define META1_BASE       0x08003800u   /* page 7, 2 KB */
#define META1_SIZE       0x800u
#define META1_PAGE       7u
#define SLOT_A_BASE      0x08004000u   /* pages 8-11, 8 KB */
#define SLOT_A_PAGE      8u
#define SLOT_B_BASE      0x08006000u   /* pages 12-15, 8 KB */
#define SLOT_B_PAGE      12u
#define SLOT_SIZE        0x2000u
#define SLOT_PAGES       4u

#define HDR_SIZE         0x100u        /* 256 B header: VTOR needs the 48-word table 256-aligned */
#define PAYLOAD_MAX      (SLOT_SIZE - HDR_SIZE)   /* 7936 */
#define SLOT_BASE(s)     ((s) ? SLOT_B_BASE : SLOT_A_BASE)
#define SLOT_PAGE(s)     ((s) ? SLOT_B_PAGE : SLOT_A_PAGE)
#define PAYLOAD_ADDR(s)  (SLOT_BASE(s) + HDR_SIZE)
#define PROG_UNIT        8u            /* 64-bit double-word programming */

/* Only these pages may ever be erased by the firmware (never the BL, never mass erase) */
#define PAGE_MIN_ERASABLE  6u
#define PAGE_MAX_ERASABLE  15u

/* RAM: 12 KB SRAM */
#define RAM_BASE         0x20000000u
#define RAM_SIZE         0x3000u
#define NOINIT_BASE      0x20000000u   /* 256 B, never zeroed (SPEC 6.3) */
#define NOINIT_SIZE      0x100u

/* Image header (SPEC 2, SPEC-C031C6 2) */
#define IMG_MAGIC        0x4D495A54u   /* "TZIM" */
#define IMG_HDR_VERSION  1u
#define BOARD_ID         0xC031C600u
#define HDR_CRC_OFF      252u

/* Meta records (SPEC 4.1) */
#define META_MAGIC       0x4154454Du   /* "META" */
#define META_REC_SIZE    32u

#endif
