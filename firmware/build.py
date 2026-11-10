#!/usr/bin/env python3
"""
Builds the test firmware (Windows + Linux, no make needed).

    python firmware/build.py          # makes build/stm32bench-fw.elf/.hex/.bin
    python firmware/build.py --json   # print the result as JSON (for agents)

If arm-none-eabi-gcc is not on PATH, it looks in the default install locations
of STM32CubeCLT and the Arm GNU Toolchain.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
NAME = "stm32bench-fw"
SOURCES = ["startup.c", "main.c"]

GCC_GLOBS = [
    r"C:\ST\STM32CubeCLT*\GNU-tools-for-STM32\bin\arm-none-eabi-gcc.exe",
    r"C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
    r"C:\Program Files\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
    "/opt/st/stm32cubeclt*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc",
]

CFLAGS = [
    "-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft",
    "-Os", "-g3", "-std=c11",
    "-Wall", "-Wextra", "-Werror",
    "-ffreestanding", "-fno-builtin", "-fno-tree-loop-distribute-patterns",
    "-ffunction-sections", "-fdata-sections",
]
LDFLAGS = ["-nostdlib", "-nostartfiles", "-Wl,--gc-sections", f"-Wl,-Map={BUILD / (NAME + '.map')}",
           "-T", str(HERE / "stm32f4.ld")]


def find_gcc() -> str | None:
    w = shutil.which("arm-none-eabi-gcc")
    if w:
        return w
    for g in GCC_GLOBS:
        hits = sorted(glob.glob(g), reverse=True)
        if hits:
            return hits[0]
    return None


def sibling(gcc: str, tool: str) -> str:
    p = Path(gcc)
    return str(p.with_name(p.name.replace("gcc", tool)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    def done(ok: bool, summary: str, **extra) -> int:
        r = {"ok": ok, "command": "build", "summary": summary, **extra}
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(("[OK  ] " if ok else "[FAIL] ") + summary)
            if extra.get("output"):
                print(extra["output"])
        return 0 if ok else 1

    gcc = find_gcc()
    if not gcc:
        return done(False, "arm-none-eabi-gcc not found.", error_type="tool_not_found", likely_causes=[
            "Install STM32CubeCLT (it includes the compiler)",
            "or install the Arm GNU Toolchain and add its bin folder to PATH",
            "Linux: sudo apt install gcc-arm-none-eabi"])

    BUILD.mkdir(exist_ok=True)
    elf = BUILD / f"{NAME}.elf"
    cmd = [gcc, *CFLAGS, *[str(HERE / s) for s in SOURCES], *LDFLAGS, "-lgcc", "-o", str(elf)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        return done(False, "Build failed.", error_type="compile_error",
                    output=(p.stdout + p.stderr).strip())

    objcopy, size = sibling(gcc, "objcopy"), sibling(gcc, "size")
    subprocess.run([objcopy, "-O", "ihex", str(elf), str(BUILD / f"{NAME}.hex")], check=True)
    subprocess.run([objcopy, "-O", "binary", str(elf), str(BUILD / f"{NAME}.bin")], check=True)
    sz = subprocess.run([size, str(elf)], capture_output=True, text=True).stdout.strip()

    warnings = (p.stdout + p.stderr).strip()
    return done(True, f"{elf.name} ready.", elf=str(elf), size=sz, warnings=warnings or None)


if __name__ == "__main__":
    sys.exit(main())
