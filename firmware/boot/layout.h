/*
 * Flash/RAM layout of the bootloader stress-test system (public/stress/SPEC.md section 1).
 * Mirrored in stress/layout.py and in firmware/boot/mkimage.py. Keep them in sync.
 */
#ifndef LAYOUT_H
#define LAYOUT_H

/* F407 sectors */
#define BL_BASE          0x08000000u   /* S0, 16 KB */
#define BL_SIZE          0x4000u
#define META0_BASE       0x08004000u   /* S1, 16 KB */
#define META0_SIZE       0x4000u
#define META0_SECTOR     1u
#define SLOT_A_BASE      0x08008000u   /* S2, 16 KB */
#define SLOT_A_SECTOR    2u
#define SLOT_B_BASE      0x0800C000u   /* S3, 16 KB */
#define SLOT_B_SECTOR    3u
#define SLOT_SIZE        0x4000u
#define META1_BASE       0x08010000u   /* S4, 64 KB */
#define META1_SIZE       0x10000u
#define META1_SECTOR     4u

#define HDR_SIZE         0x200u        /* 512 B header in front of every payload */
#define PAYLOAD_MAX      (SLOT_SIZE - HDR_SIZE)   /* 15872 */
#define SLOT_BASE(s)     ((s) ? SLOT_B_BASE : SLOT_A_BASE)
#define SLOT_SECTOR(s)   ((s) ? SLOT_B_SECTOR : SLOT_A_SECTOR)
#define PAYLOAD_ADDR(s)  (SLOT_BASE(s) + HDR_SIZE)

/* Only these sectors may ever be erased by the firmware (never S0, never mass erase) */
#define SECTOR_MIN_ERASABLE  1u
#define SECTOR_MAX_ERASABLE  4u

/* RAM */
#define RAM_BASE         0x20000000u
#define RAM_SIZE         0x20000u      /* 128 KB SRAM (CCM unused) */
#define NOINIT_BASE      0x20000000u   /* 256 B, never zeroed (section 6.3) */
#define NOINIT_SIZE      0x100u

/* Image header (section 2) */
#define IMG_MAGIC        0x4D495A54u   /* "TZIM" */
#define IMG_HDR_VERSION  1u
#define BOARD_ID         0xF407D15Cu

/* Meta records (section 4.1) */
#define META_MAGIC       0x4154454Du   /* "META" */
#define META_REC_SIZE    32u

#endif
