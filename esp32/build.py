#!/usr/bin/env python3
"""Compile the ESP32-S3 bridge (esp32/bridge, docs/stress/SPEC-ESP32.md 2) with arduino-cli.

    python3 esp32/build.py            # esp32/build/bridge/bridge.ino.{bin,bootloader.bin,partitions.bin,merged.bin}
    python3 esp32/build.py --json     # the result as JSON (ok, error_type, summary, likely_causes, binaries)

Compile only: this script never uploads. The upload command for a human is in the JSON
(`upload_hint`) and in esp32/bridge/README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKETCH = HERE / "bridge"
BUILD = HERE / "build" / "bridge"
# ESP32-S3, 16 MB flash, 8 MB octal PSRAM (N16R8); USB CDC on boot disabled, so Serial = UART0 = the
# CH343 "UART" USB port.
FQBN = "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=default,PartitionScheme=app3M_fat9M_16MB"
CORE = "esp32:esp32"
OUTPUTS = ("bridge.ino.bin", "bridge.ino.bootloader.bin", "bridge.ino.partitions.bin", "bridge.ino.merged.bin")


def find_cli() -> str | None:
    w = shutil.which("arduino-cli")
    if w:
        return w
    for c in (Path.home() / ".local/bin/arduino-cli", Path("/usr/local/bin/arduino-cli")):
        if c.is_file():
            return str(c)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fqbn", default=FQBN)
    args = ap.parse_args(argv)

    def done(ok: bool, summary: str, error_type: str | None = None, causes=(), **extra) -> int:
        r = {"ok": ok, "command": "esp32-build", "error_type": error_type, "summary": summary,
             "likely_causes": list(causes), **extra}
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(("[OK  ] " if ok else "[FAIL] ") + summary)
            for c in causes:
                print("       - " + c)
            for b in extra.get("binaries", []):
                print(f"       {b['path']} ({b['size']} B)")
        return 0 if ok else 1

    cli = find_cli()
    if cli is None:
        return done(False, "arduino-cli not found.", "tool_missing",
                    ["Install arduino-cli (https://arduino.github.io/arduino-cli/) and put it on PATH"])
    cores = subprocess.run([cli, "core", "list"], capture_output=True, text=True, timeout=60)
    if CORE not in cores.stdout:
        return done(False, f"The {CORE} core is not installed.", "core_missing",
                    [f"arduino-cli core install {CORE}"], core_list=cores.stdout.strip())
    BUILD.mkdir(parents=True, exist_ok=True)
    cmd = [cli, "compile", "--fqbn", args.fqbn, "--warnings", "all", "--build-path", str(BUILD), str(SKETCH)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return done(False, "arduino-cli compile timed out (15 min).", "timeout")
    log = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        errs = [ln for ln in log.splitlines() if "error" in ln.lower()][:10]
        return done(False, "Compile failed.", "compile_failed", errs or ["See details"], details=log[-4000:],
                    command=" ".join(cmd))
    warnings = [ln for ln in log.splitlines() if "bridge.ino" in ln and "warning" in ln.lower()]
    bins = []
    for name in OUTPUTS:
        f = BUILD / name
        if f.is_file():
            bins.append({"name": name, "path": str(f), "size": f.stat().st_size,
                         "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
    if not any(b["name"] == "bridge.ino.bin" for b in bins):
        return done(False, "Compile reported success but bridge.ino.bin is missing.", "output_missing",
                    details=log[-2000:])
    size_line = next((ln for ln in log.splitlines() if ln.startswith("Sketch uses")), "")
    port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_<serial>-if00"
    return done(True, f"Built tezgah-bridge for {args.fqbn.split(':')[2]}. {size_line}".strip(),
                fqbn=args.fqbn, build_dir=str(BUILD), binaries=bins, warnings=warnings,
                upload_hint=(f"arduino-cli upload --fqbn {args.fqbn} --input-dir {BUILD} -p {port} {SKETCH}  "
                             "(human only; see esp32/bridge/README.md)"))


if __name__ == "__main__":
    sys.exit(main())
