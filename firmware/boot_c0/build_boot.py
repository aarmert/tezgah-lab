#!/usr/bin/env python3
"""
Builds the NUCLEO-C031C6 bootloader stress-test firmware (public/stress/SPEC.md +
public/stress/SPEC-C031C6.md + public/stress/SPEC-ESP32.md 1) into firmware/boot_c0/build/:

  bl_ab, bl_ab_hooks, bl_naive, bl_naive_hooks         (.elf/.bin/.map, pages 0-5 @ 0x08000000)
  app_<variant>_<A|B>[_nohooks]                        (.elf/.bin/.map, slot_base + 0x100)
  images/          every image of SPEC 2 / SPEC-C031C6 2, golden_*.bin/.hex, manifest.json
  images/release/  the same image set built without FAULT_HOOKS (release BL + release apps)

    python firmware/boot_c0/build_boot.py          # human-readable
    python firmware/boot_c0/build_boot.py --json   # ok, summary, error_type, likely_causes (+ sizes)

Warnings are errors (-Werror, and any linker warning fails the build). The build fails if a BL is
larger than 12 KB or an app payload is larger than 8192 - 256 bytes.

wrong_board_{A,B}.img embeds the real F407 v2 app from firmware/boot/build/. If that is missing,
this script runs firmware/boot/build_boot.py first (it does not change any F407 source).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
sys.path.insert(0, str(HERE))
import mkimage  # noqa: E402

# Reuse the toolchain lookup of firmware/build.py (that file stays unchanged)
_spec = importlib.util.spec_from_file_location("fw_build", HERE.parent / "build.py")
fw_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw_build)

CFLAGS = [
    "-mcpu=cortex-m0plus", "-mthumb", "-mno-unaligned-access",
    "-Os", "-g3", "-std=c11",
    "-Wall", "-Wextra", "-Werror",
    "-ffreestanding", "-fno-builtin", "-fno-tree-loop-distribute-patterns",
    "-ffunction-sections", "-fdata-sections",
    f"-I{HERE}",
]

COMMON_SRC = ["startup_boot.c", "hw.c", "crc32.c", "flash.c", "noinit.c", "image.c", "fault.c"]
F407_BOOT = HERE.parent / "boot"
BL_SRC = {"ab": COMMON_SRC + ["b64.c", "meta.c", "sha256.c", "bl.c", "bl_update.c"],
          "naive": COMMON_SRC + ["b64.c", "bl.c", "bl_update.c"]}
APP_SRC = COMMON_SRC + ["meta.c", "app_rt.c", "app.c"]

BL_MAX = mkimage.BL_SIZE
APP_MAX = mkimage.PAYLOAD_MAX


def targets() -> list[dict]:
    t = []
    for design in ("ab", "naive"):
        for hooks in (False, True):
            defs = (["DESIGN_NAIVE"] if design == "naive" else []) + (["FAULT_HOOKS"] if hooks else [])
            t.append({"name": f"bl_{design}" + ("_hooks" if hooks else ""), "kind": "bl",
                      "src": BL_SRC[design], "defs": defs,
                      "origin": mkimage.BL_BASE, "length": BL_MAX, "max": BL_MAX})
    for var, m in mkimage.APPS.items():
        for i, slot in enumerate(mkimage.SLOTS):
            for hooks in (True, False):     # app_<var>_<slot> (hooks) and app_<var>_<slot>_nohooks (release)
                defs = (["FAULT_HOOKS"] if hooks else []) + [f"APP_SLOT={i}", f'APP_VERSION="{m["version"]}"',
                                                             f'APP_SUFFIX="{m["suffix"]}"']
                if m["define"]:
                    defs.append(m["define"])
                t.append({"name": f"app_{var}_{slot}" + ("" if hooks else "_nohooks"), "kind": "app",
                          "src": APP_SRC, "defs": defs,
                          "origin": mkimage.payload_addr(slot), "length": APP_MAX, "max": APP_MAX})
    return t


def section_sizes(size_tool: str, elf: Path) -> dict[str, int]:
    out = subprocess.run([size_tool, "-A", str(elf)], capture_output=True, text=True).stdout
    s = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith(".") and parts[1].isdigit():
            s[parts[0]] = int(parts[1])
    return s


def build_one(gcc: str, t: dict) -> dict:
    name = t["name"]
    ld = BUILD / f"{name}.ld"
    ld.write_text((HERE / "link.ld.in").read_text()
                  .replace("@FLASH_ORIGIN@", f"0x{t['origin']:08X}")
                  .replace("@FLASH_LENGTH@", str(t["length"])))
    elf = BUILD / f"{name}.elf"
    cmd = [gcc, *CFLAGS, *[f"-D{d}" for d in t["defs"]], *[str(HERE / s) for s in t["src"]],
           "-nostdlib", "-nostartfiles", "-Wl,--gc-sections", "-Wl,--no-warn-rwx-segments",
           f"-Wl,-Map={BUILD / (name + '.map')}", "-T", str(ld), "-lgcc", "-o", str(elf)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    out = (p.stdout + p.stderr).strip()
    r = {"name": name, "ok": p.returncode == 0 and "warning" not in out.lower(), "output": out}
    if not r["ok"]:
        return r
    bin_ = BUILD / f"{name}.bin"
    subprocess.run([fw_build.sibling(gcc, "objcopy"), "-O", "binary", str(elf), str(bin_)], check=True)
    secs = section_sizes(fw_build.sibling(gcc, "size"), elf)
    flash = bin_.stat().st_size
    ram = sum(secs.get(k, 0) for k in (".noinit", ".ramvec", ".ramfunc", ".data", ".bss"))
    r.update(flash=flash, flash_max=t["max"], ram_static=ram,
             ramfunc=secs.get(".ramfunc", 0))
    if flash > t["max"]:
        r["ok"] = False
        r["output"] = f"{name}: {flash} bytes > limit {t['max']}"
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    def done(ok: bool, summary: str, **extra) -> int:
        r = {"ok": ok, "command": "build_boot", "summary": summary,
             "error_type": extra.pop("error_type", None), "likely_causes": extra.pop("likely_causes", []),
             **extra}
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(("[OK  ] " if ok else "[FAIL] ") + summary)
            for s in r.get("sizes", {}).values():
                print(f"  {s['name']:<22} flash {s['flash']:>6} / {s['flash_max']}   "
                      f"RAM {s['ram_static']:>5} (ramfunc {s['ramfunc']})")
            if extra.get("output"):
                print(extra["output"])
        return 0 if ok else 1

    gcc = fw_build.find_gcc()
    if not gcc:
        return done(False, "arm-none-eabi-gcc not found.", error_type="tool_not_found", likely_causes=[
            "Install STM32CubeCLT (it ships the compiler)",
            "or install the Arm GNU Toolchain and add its bin folder to PATH",
            "Linux: sudo apt install gcc-arm-none-eabi"])

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    with ThreadPoolExecutor() as ex:
        results = list(ex.map(lambda t: build_one(gcc, t), targets()))

    failed = [r for r in results if not r["ok"]]
    if failed:
        too_big = [r for r in failed if "> limit" in r["output"]]
        return done(False, f"{len(failed)} of {len(results)} targets failed: "
                    + ", ".join(r["name"] for r in failed),
                    error_type="size_limit" if too_big and len(too_big) == len(failed) else "compile_error",
                    likely_causes=["Compiler error or warning (warnings are errors)",
                                   "Image larger than its flash region (BL 12 KB, app 8192-256)"],
                    output="\n\n".join(f"== {r['name']} ==\n{r['output']}" for r in failed))

    sizes = {r["name"]: {k: r[k] for k in ("name", "flash", "flash_max", "ram_static", "ramfunc")}
             for r in results}
    if not all((mkimage.F407_BUILD / f"app_v2_{s}.bin").is_file() for s in mkimage.SLOTS):
        p = subprocess.run([sys.executable, str(F407_BOOT / "build_boot.py"), "--json"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return done(False, "F407 build (needed for wrong_board) failed.", error_type="f407_build_error",
                        likely_causes=["firmware/boot does not build: see its own build_boot.py output"],
                        output=(p.stdout + p.stderr)[-4000:], sizes=sizes)
    try:
        manifest = mkimage.build_all(BUILD)
    except Exception as e:  # noqa: BLE001
        return done(False, f"mkimage failed: {e}", error_type="mkimage_error",
                    likely_causes=["Payload too large", "Missing build output"], sizes=sizes)

    return done(True, f"{len(results)} ELFs and {len(manifest)} image files built (target c031).",
                target=mkimage.TARGET,
                build_dir=str(BUILD), images_dir=str(BUILD / "images"),
                manifest=str(BUILD / "images" / "manifest.json"), sizes=sizes)


if __name__ == "__main__":
    sys.exit(main())
