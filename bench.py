#!/usr/bin/env python3
"""
tezgah - STM32 test bench tool (Windows + Linux)

Flashes a board, resets it, talks to it over the serial port and runs
test plans. Both a human and an AI agent (with --json) can use it.

Quick start:
    python bench.py doctor          # is everything ready?
    python bench.py ports           # list serial ports
    python bench.py run smoke       # run the plans/smoke.toml plan
    python bench.py --sim run smoke # try it with a simulated board, no hardware

Requires: Python 3.11+, pyserial, STM32CubeProgrammer CLI (or OpenOCD).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("tezgah: Python 3.11 or newer is required (for tomllib).")

import argparse
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.3.1"
IS_WIN = os.name == "nt"
SCRIPT_DIR = Path(__file__).resolve().parent

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Error and result structure
# ---------------------------------------------------------------------------

class BenchError(Exception):
    """Structured error that an agent can understand."""

    def __init__(self, error_type: str, summary: str,
                 likely_causes: list[str] | None = None, details: str | None = None):
        super().__init__(summary)
        self.error_type = error_type
        self.summary = summary
        self.likely_causes = likely_causes or []
        self.details = details


def ok_result(command: str, summary: str, **extra) -> dict:
    return {"ok": True, "command": command, "summary": summary, **extra}


def err_result(command: str, e: BenchError, **extra) -> dict:
    r = {"ok": False, "command": command, "error_type": e.error_type,
         "summary": e.summary, "likely_causes": e.likely_causes, **extra}
    if e.details:
        r["details"] = e.details
    return r


def emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    mark = "OK  " if result.get("ok") else "FAIL"
    print(f"[{mark}] {result.get('summary', '')}")
    if not result.get("ok"):
        if result.get("error_type"):
            print(f"       type: {result['error_type']}")
        for c in result.get("likely_causes", []):
            print(f"       likely cause: {c}")
        if result.get("details"):
            print("       details:")
            for line in str(result["details"]).splitlines()[-15:]:
                print(f"         {line}")
    for key in ("lines", "response"):
        if result.get(key):
            print("       serial:")
            for line in result[key]:
                print(f"         {line}")
    if result.get("report_dir"):
        print(f"       report: {result['report_dir']}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "bench": {"name": "bench"},
    "probe": {"backend": "cubeprog", "connect_mode": "NORMAL", "timeout_s": 90},
    "serial": {"port": "", "baud": 115200},
    "recovery": {"golden_image": "firmware/build/stm32bench-fw.elf",
                 "banner": "^BOOT ", "banner_timeout_s": 3},
    "permissions": {"allow_flash": True, "allow_reset": True, "plans_dir_only": False},
    "paths": {"plans_dir": "plans", "runs_dir": "runs"},
    # cam_blink: video via camera/ (the phone camera tool). If roi = [x, y, width, height] (pixels) is given,
    # the LED region is not searched for automatically.
    "camera": {"tool_dir": "camera", "bun": "~/.bun/bin/bun", "serial": "",
               "camera": "back", "fps": 30, "roi": None},
}


@dataclass
class Config:
    path: Path | None
    home: Path
    data: dict

    def get(self, section: str, key: str, default=None):
        sec = self.data.get(section, {})
        if key in sec:
            return sec[key]
        return DEFAULT_CONFIG.get(section, {}).get(key, default)

    def resolve(self, p: str | Path) -> Path:
        p = Path(os.path.expanduser(str(p)))
        return p if p.is_absolute() else (self.home / p)

    @property
    def runs_dir(self) -> Path:
        d = self.resolve(self.get("paths", "runs_dir"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def plans_dir(self) -> Path:
        return self.resolve(self.get("paths", "plans_dir"))


def load_config(arg: str | None, sim: bool) -> Config:
    candidates = []
    if arg:
        candidates.append(Path(arg))
    if os.environ.get("BENCH_CONFIG"):
        candidates.append(Path(os.environ["BENCH_CONFIG"]))
    candidates.append(SCRIPT_DIR / "bench.toml")

    for c in candidates:
        c = Path(os.path.expanduser(str(c)))
        if c.is_file():
            try:
                data = tomllib.loads(c.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                raise BenchError("config_invalid", f"Could not read {c}: {e}",
                                 ["TOML syntax error (quotes, square brackets)"])
            cfg = Config(c.resolve(), c.resolve().parent, data)
            break
    else:
        if not sim:
            raise BenchError(
                "config_not_found", "bench.toml not found.",
                ["Copy bench.example.toml to bench.toml and set the serial port",
                 "If it is somewhere else, point to it with --config or BENCH_CONFIG"])
        cfg = Config(None, SCRIPT_DIR, {})

    if sim:
        cfg.data.setdefault("probe", {})["backend"] = "sim"
        cfg.data.setdefault("serial", {})["port"] = "sim"
    return cfg


# ---------------------------------------------------------------------------
# Audit log (SHA-256 chained) and lock
# ---------------------------------------------------------------------------

def audit_append(cfg: Config, entry: dict) -> str:
    path = cfg.runs_dir / "audit.jsonl"
    prev = "0" * 64
    if path.exists():
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            try:
                prev = json.loads(lines[-1])["hash"]
            except Exception:
                prev = "corrupt"
    body = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **entry, "prev": prev}
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False)
    body["hash"] = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(body, ensure_ascii=False) + "\n")
    return body["hash"]


def audit_verify(cfg: Config) -> dict:
    path = cfg.runs_dir / "audit.jsonl"
    if not path.exists():
        return ok_result("audit", "No entries yet.", entries=0)
    prev = "0" * 64
    n = 0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        h = rec.pop("hash", None)
        if rec.get("prev") != prev:
            raise BenchError("audit_broken", f"Audit chain broken at line {i} (prev does not match).",
                             ["The log file was edited by hand or a line was deleted"])
        payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        if hashlib.sha256((prev + payload).encode("utf-8")).hexdigest() != h:
            raise BenchError("audit_broken", f"Audit chain corrupt at line {i} (hash does not match).",
                             ["The content of this line was changed afterwards"])
        prev = h
        n += 1
    return ok_result("audit", f"Audit chain intact ({n} entries).", entries=n)


class BenchLock:
    """Keeps two processes from accessing the board at the same time (Windows + Linux).

    Uses the OS file lock (Linux: flock, Windows: msvcrt.locking). The process holding
    the lock keeps it however long it runs; if the process dies (crash, kill, Ctrl+C),
    the OS releases the lock itself. So there is no "stale lock" guessing and no file
    deletion. The lock file is never deleted; it only holds the PID of the holder,
    for information.
    """

    def __init__(self, cfg: Config, wait_s: float = 30):
        self.path = cfg.runs_dir / ".bench.lock"
        self.wait_s = wait_s
        self.f = None

    def _try_lock(self) -> bool:
        try:
            if IS_WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "?"
        except OSError:
            return "?"

    def __enter__(self):
        self.f = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.wait_s
        while not self._try_lock():
            if time.monotonic() > deadline:
                holder = self._holder()
                self.f.close()
                self.f = None
                raise BenchError("bench_busy", f"The bench is in use by another process (pid {holder}).",
                                 ["Another bench.py command is running (monitor, read or gdbserver may have been left open)",
                                  "The lock is released when that process ends; no need to delete the file"])
            time.sleep(0.3)
        self.f.seek(0)
        self.f.truncate()
        self.f.write(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
        self.f.flush()
        return self

    def __exit__(self, *exc):
        if self.f is None:
            return
        try:
            if IS_WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()  # on Linux the flock is released when the file is closed
        self.f = None


# ---------------------------------------------------------------------------
# Finding and running external tools
# ---------------------------------------------------------------------------

CUBEPROG_GLOBS = [
    r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
    r"C:\ST\STM32CubeCLT*\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
] if IS_WIN else [
    "/opt/st/stm32cubeclt*/STM32CubeProgrammer/bin/STM32_Programmer_CLI",
    "~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI",
    "/usr/local/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI",
]

OPENOCD_GLOBS = [r"C:\Program Files\OpenOCD*\bin\openocd.exe", r"C:\openocd*\bin\openocd.exe"] if IS_WIN else []


def find_tool(configured: str | None, names: list[str], globs: list[str]) -> str | None:
    if configured:
        p = Path(os.path.expanduser(configured))
        if p.is_file():
            return str(p)
        w = shutil.which(configured)
        return w
    for n in names:
        w = shutil.which(n)
        if w:
            return w
    for g in globs:
        hits = sorted(glob.glob(os.path.expanduser(g)), reverse=True)
        if hits:
            return hits[0]
    return None


def run_tool(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise BenchError("tool_not_found", f"Program not found: {cmd[0]}",
                         ["Not installed or not on PATH", "Give the full path in bench.toml"])
    except subprocess.TimeoutExpired:
        raise BenchError("timeout", f"{Path(cmd[0]).name} did not finish in {timeout:.0f} s and was stopped.",
                         ["The probe is stuck: unplug and replug the USB cable",
                          "The board has no power or the power is unstable"])
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# ---------------------------------------------------------------------------
# Programmers: STM32CubeProgrammer, OpenOCD, Sim
# ---------------------------------------------------------------------------

CAUSES_PROBE = ["USB cable not plugged in, or it is a charge-only cable",
                "Windows: ST-Link driver not installed (the STM32CubeProgrammer installer includes it)",
                "Linux: no ST-Link udev rule, or the user is not in the plugdev group",
                "The probe is open in another program (CubeIDE, CubeProgrammer GUI, OpenOCD)"]
CAUSES_TARGET = ["The board has no power",
                 "The firmware uses the SWD pins (PA13/PA14) for something else: run 'bench.py recover'",
                 "The chip is in sleep mode or locked up: run 'bench.py recover'",
                 "The CN3 jumpers on the Discovery board are not fitted"]


class Programmer:
    name = "base"

    def flash(self, image: Path, under_reset: bool = False) -> str: ...
    def reset(self) -> str: ...
    def probe_check(self) -> str: ...

    def debug_commands(self, cmds: list[str], timeout: float = 30) -> str:
        """Debug commands (halt, reg, mdw). Only OpenOCD supports them for now."""
        raise BenchError("not_supported", f"This command is not supported by the '{self.name}' backend.",
                         ["Set backend = \"openocd\" in bench.toml [probe] (OpenOCD can be installed on Windows too)"])

    def tool_version(self) -> str:
        """Version of the programmer tool (for the report header)."""
        exe = getattr(self, "exe", None)
        if not exe:
            return "?"
        try:
            rc, out = run_tool([exe, "--version"], 10)
        except BenchError:
            return "?"
        m = re.search(r"(?:Open On-Chip Debugger|version:?)\s+(\S+)", out, re.I)
        return m.group(1) if m else "?"

    def read_uid(self) -> str:
        """Reads the chip's 96-bit UID over the debug port (the core is not halted)."""
        words = parse_mdw(self.debug_commands([f"mdw 0x{UID_ADDR:08X} 3"]))
        if len(words) != 3:
            raise BenchError("tool_error", f"Asked for 3 words for the UID, read {len(words)}.")
        return "".join(f"{v:08X}" for _, v in words)


class CubeProgrammer(Programmer):
    name = "cubeprog"

    def __init__(self, cfg: Config):
        self.exe = find_tool(cfg.get("probe", "cubeprog_path"),
                             ["STM32_Programmer_CLI", "STM32_Programmer_CLI.exe"], CUBEPROG_GLOBS)
        self.mode = cfg.get("probe", "connect_mode")
        self.sn = cfg.get("probe", "serial_number")
        self.timeout = float(cfg.get("probe", "timeout_s"))
        if not self.exe:
            raise BenchError("tool_not_found", "STM32_Programmer_CLI not found.",
                             ["STM32CubeProgrammer or STM32CubeCLT is not installed",
                              "If it is installed, give the full path with cubeprog_path in bench.toml [probe]"])

    def _conn(self, mode: str) -> list[str]:
        c = ["-c", "port=SWD", f"mode={mode}"]
        if self.sn:
            c.append(f"sn={self.sn}")
        return c

    @staticmethod
    def _classify(rc: int, out: str, verify: bool) -> None:
        low = out.lower()
        if re.search(r"no (st-?link|debug probe) detected|no st-?link found|unable to open probe|dev_usb_comm_err", low):
            raise BenchError("probe_not_found", "ST-Link probe not found.", CAUSES_PROBE, tail(out))
        if re.search(r"no stm32 target found|unable to get core id|target not found|dev_connect_err|failed to connect", low):
            raise BenchError("target_not_responding", "ST-Link found, but the target chip does not respond.",
                             CAUSES_TARGET, tail(out))
        if re.search(r"file .*(not found|does not exist)|cannot open file|wrong file", low):
            raise BenchError("image_not_found", "The image to flash was not found or could not be read.",
                             ["Build the firmware first: python firmware/build.py"], tail(out))
        if re.search(r"read.?out protection|rdp", low) and rc != 0:
            raise BenchError("read_protected", "Read-out protection (RDP) is active on the chip.",
                             ["This tool deliberately does not touch option bytes; a human should check with CubeProgrammer"],
                             tail(out))
        if re.search(r"verif\w* (failed|error)|data mismatch|mismatch found", low):
            raise BenchError("verify_failed", "Verification after writing failed.",
                             ["The flash sector is write-protected", "Unstable power", "Loose cable/connection"],
                             tail(out))
        err_lines = [l.strip() for l in out.splitlines() if re.match(r"\s*error", l, re.I)]
        if rc != 0 or err_lines:
            raise BenchError("tool_error", err_lines[0] if err_lines else f"STM32_Programmer_CLI exit code {rc}",
                             ["See details; try 'bench.py recover' if needed"], tail(out))

    def flash(self, image: Path, under_reset: bool = False) -> str:
        mode = "UR" if under_reset else self.mode
        rc, out = run_tool([self.exe, *self._conn(mode), "-w", str(image), "-v", "-rst"], self.timeout)
        self._classify(rc, out, verify=True)
        return f"{image.name} flashed and verified (mode={mode})."

    def reset(self) -> str:
        rc, out = run_tool([self.exe, *self._conn(self.mode), "-rst"], 30)
        self._classify(rc, out, verify=False)
        return "Board reset."

    def probe_check(self) -> str:
        rc, out = run_tool([self.exe, "-l", "st-link"], 30)
        sns = re.findall(r"ST-?LINK SN\s*:\s*(\S+)", out, re.I)
        if not sns:
            raise BenchError("probe_not_found", "No ST-Link connected.", CAUSES_PROBE, tail(out))
        return f"ST-Link found: {', '.join(sns)}"


class OpenOCD(Programmer):
    name = "openocd"

    def __init__(self, cfg: Config):
        self.exe = find_tool(cfg.get("probe", "openocd_path"), ["openocd", "openocd.exe"], OPENOCD_GLOBS)
        self.interface = cfg.get("probe", "openocd_interface", "interface/stlink.cfg")
        self.target = cfg.get("probe", "openocd_target", "target/stm32f4x.cfg")
        self.sn = cfg.get("probe", "serial_number")
        self.timeout = float(cfg.get("probe", "timeout_s"))
        if not self.exe:
            raise BenchError("tool_not_found", "openocd not found.",
                             ["Linux: sudo apt install openocd", "Give the path with openocd_path in bench.toml [probe]"])

    def _base(self, under_reset: bool) -> list[str]:
        c = [self.exe, "-f", self.interface, "-f", self.target]
        if self.sn:
            c += ["-c", f"adapter serial {self.sn}"]
        if under_reset:
            c += ["-c", "reset_config srst_only srst_nogate connect_assert_srst"]
        return c

    @staticmethod
    def _classify(rc: int, out: str) -> None:
        low = out.lower()
        if re.search(r"open failed|unable to open|no device found|libusb_error|could not find", low):
            raise BenchError("probe_not_found", "ST-Link probe not found.", CAUSES_PROBE, tail(out))
        if re.search(r"init mode failed|unable to connect to the target|error connecting dp|target not examined", low):
            raise BenchError("target_not_responding", "Probe found, but the target chip does not respond.",
                             CAUSES_TARGET, tail(out))
        if re.search(r"couldn't open|no such file", low):
            raise BenchError("image_not_found", "Could not open the image to flash.", [], tail(out))
        if re.search(r"verify failed|checksum mismatch", low):
            raise BenchError("verify_failed", "Verification failed.", [], tail(out))
        if rc != 0:
            raise BenchError("tool_error", f"openocd exit code {rc}", [], tail(out))

    def flash(self, image: Path, under_reset: bool = False) -> str:
        cmd = self._base(under_reset) + ["-c", f"program {{{image.resolve().as_posix()}}} verify reset exit"]
        rc, out = run_tool(cmd, self.timeout)
        self._classify(rc, out)
        return f"{image.name} flashed and verified."

    def reset(self) -> str:
        rc, out = run_tool(self._base(False) + ["-c", "init; reset run; shutdown"], 30)
        self._classify(rc, out)
        return "Board reset."

    def probe_check(self) -> str:
        rc, out = run_tool(self._base(False) + ["-c", "init; shutdown"], 30)
        self._classify(rc, out)
        return "OpenOCD connected to the probe and the target."

    def debug_commands(self, cmds: list[str], timeout: float = 30) -> str:
        cmd = self._base(False) + ["-c", "init"]
        for c in cmds:
            cmd += ["-c", c]
        rc, out = run_tool(cmd + ["-c", "shutdown"], timeout)
        self._classify(rc, out)
        return out

    def gdb_server_cmd(self, port: int) -> list[str]:
        # Listens on localhost only (OpenOCD 0.12 default); telnet/tcl disabled
        return self._base(False) + ["-c", f"gdb_port {port}", "-c", "telnet_port disabled",
                                    "-c", "tcl_port disabled"]


# --- Simulated board: for trying the tool and plans without hardware -------

SIM_UID = "0033002A3438511234363632"
SIM_FW_VERSION = "1.2.0"


class SimBoard:
    """Mimics the test firmware; the state is kept in runs/sim_state.json."""

    def __init__(self, cfg: Config):
        self.state_path = cfg.runs_dir / "sim_state.json"
        st = {}
        if self.state_path.exists():
            try:
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                st = {}
        self.image = st.get("image")
        self.image_path = st.get("image_path")
        self.crashed = st.get("crashed", False)
        self.boot_t = st.get("boot_t", time.time())
        self.green = st.get("green", 2.0)   # green LED: blink rate in Hz, "on" or "off"
        self.out: list[str] = []

    def _save(self):
        self.state_path.write_text(json.dumps(
            {"image": self.image, "image_path": self.image_path, "crashed": self.crashed,
             "boot_t": self.boot_t, "green": self.green}), encoding="utf-8")

    def flash(self, image: Path):
        if not image.is_file():
            raise BenchError("image_not_found", f"{image} not found.",
                             ["Build the firmware first: python firmware/build.py"])
        self.image = image.name
        self.image_path = str(image.resolve())
        self.reset("PIN,SFT")

    def reset(self, cause="PIN,SFT"):
        self.crashed = False
        self.boot_t = time.time()
        self.green = 2.0
        if self.image:
            self.out.append(f"BOOT stm32bench-fw v{SIM_FW_VERSION} reset={cause}")
        self._save()

    def write_video(self, dest: Path, seconds: float):
        """Synthetic video (30 fps) that mimics the green LED: grey background, a blinking square where the LED is."""
        if isinstance(self.green, float):
            half = 0.5 / self.green
            enable = f"enable='lt(mod(t,{2 * half:.6f}),{half:.6f})'"
        else:
            enable = "enable='1'" if self.green == "on" else "enable='0'"
        p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"color=c=0x404040:s=640x360:r=30:d={seconds:g},"
                            f"drawbox=x=300:y=200:w=24:h=24:c=0x30FF30:t=fill:{enable}",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise BenchError("camera_failed", "[sim] could not generate the synthetic video (ffmpeg).", [], tail(p.stderr))

    def _fault_pc(self) -> int:
        """Find the address of the 'udf' instruction in the real firmware (0 if there is none)."""
        gcc = shutil.which("arm-none-eabi-gcc")
        if not gcc or not self.image_path:
            return 0
        objdump = str(Path(gcc).with_name(Path(gcc).name.replace("gcc", "objdump")))
        try:
            out = subprocess.run([objdump, "-d", self.image_path], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0
        m = re.search(r"^\s*([0-9a-f]+):\s+de00\s+udf", out, re.M)
        return int(m.group(1), 16) if m else 0

    def handle(self, line: str):
        if not self.image or self.crashed:
            return
        cmd = line.strip().upper()
        if cmd == "PING":
            self.out.append("PONG")
        elif cmd == "VER":
            self.out.append(f"VER {SIM_FW_VERSION}")
        elif cmd == "TICK":
            self.out.append(f"TICK {int((time.time() - self.boot_t) * 1000)}")
        elif cmd == "UID":
            self.out.append(f"UID {SIM_UID}")
        elif cmd == "VDD":
            self.out.append("VDD 3000")
        elif cmd == "TEMP":
            self.out.append("TEMP 30.0")
        elif m := re.fullmatch(r"LED ([GORB]) (ON|OFF)", cmd):
            self.out.append(f"OK LED {m.group(1)} {m.group(2)}")
            if m.group(1) == "G":
                self.green = m.group(2).lower()
                self._save()
        elif m := re.fullmatch(r"BLINK (\d+)", cmd):
            hz = int(m.group(1))
            self.out.append(f"OK BLINK {hz}" if 1 <= hz <= 20 else "ERR range 1-20")
            if 1 <= hz <= 20:
                self.green = float(hz)
                self._save()
        elif cmd == "CRASH":
            self.out.append("OK CRASH")
            self.out.append(f"HARDFAULT pc=0x{self._fault_pc():08X} lr=0x080000A7 cfsr=0x00010000 hfsr=0x40000000")
            self.crashed = True
            self._save()
        elif cmd == "RESET":
            self.out.append("OK RESET")
            self.reset("SFT")
        elif cmd:
            self.out.append("ERR unknown command")


class SimProgrammer(Programmer):
    name = "sim"

    def __init__(self, cfg: Config, board: SimBoard):
        self.board = board

    def flash(self, image: Path, under_reset: bool = False) -> str:
        time.sleep(0.2)
        self.board.flash(image)
        return f"[sim] {image.name} flashed and verified."

    def reset(self) -> str:
        self.board.reset()
        return "[sim] Board reset."

    def probe_check(self) -> str:
        return "[sim] Simulated probe ready."

    def tool_version(self) -> str:
        return "sim"

    def read_uid(self) -> str:
        return SIM_UID


def make_programmer(cfg: Config, board: SimBoard | None) -> Programmer:
    backend = cfg.get("probe", "backend")
    if backend == "sim":
        return SimProgrammer(cfg, board)
    if backend == "cubeprog":
        return CubeProgrammer(cfg)
    if backend == "openocd":
        return OpenOCD(cfg)
    raise BenchError("config_invalid", f"Unknown backend: {backend}", ["Must be cubeprog, openocd or sim"])


# ---------------------------------------------------------------------------
# Serial port: real and simulated
# ---------------------------------------------------------------------------

CAUSES_SERIAL = ["Wrong port name: list them with 'bench.py ports'",
                 "The port is open in another program (PuTTY, Arduino IDE, CubeIDE serial monitor)",
                 "The USB-TTL adapter is unplugged",
                 "Linux: the user is not in the dialout group"]


class SerialTransport:
    def __init__(self, port: str, baud: int):
        try:
            import serial  # noqa: F401
        except ImportError:
            raise BenchError("pyserial_missing", "pyserial is not installed.",
                             ["Windows: py -m pip install pyserial", "Linux: sudo apt install python3-serial"])
        if not port:
            raise BenchError("config_invalid", "[serial] port is empty in bench.toml.",
                             ["Find the port with 'bench.py ports' and set it (Windows: COM5, Linux: /dev/ttyUSB0)"])
        self.port, self.baud = port, baud
        self.ser = None
        self._open()

    def _open(self):
        import serial
        try:
            # serial_for_url: supports COM5, /dev/ttyUSB0 and network socket://host:port addresses
            self.ser = serial.serial_for_url(self.port, baudrate=self.baud, timeout=0.05, write_timeout=1)
        except serial.SerialException as e:
            raise BenchError("serial_open_failed", f"Could not open {self.port}.", CAUSES_SERIAL, str(e))

    def _reopen(self):
        """Reconnect if the port disappears for a moment (USB re-enumerated)."""
        try:
            self.ser.close()
        except Exception:
            pass
        deadline = time.monotonic() + 3
        while True:
            try:
                self._open()
                return
            except BenchError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def write(self, data: bytes):
        import serial
        try:
            self.ser.write(data)
            self.ser.flush()
        except serial.SerialException:
            self._reopen()
            self.ser.write(data)

    def read_available(self, timeout: float) -> str:
        import serial
        deadline = time.monotonic() + timeout
        chunks = b""
        while True:
            try:
                n = self.ser.in_waiting
                if not n and timeout <= 0:
                    break  # read(1) would block for the port timeout (50 ms)
                data = self.ser.read(n or 1)
            except (serial.SerialException, OSError):
                self._reopen()
                data = b""
            if data:
                chunks += data
                if not self.ser.in_waiting:
                    break
            elif time.monotonic() >= deadline:
                break
        return chunks.decode("utf-8", errors="replace")

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class SimTransport:
    def __init__(self, board: SimBoard):
        self.board = board

    def write(self, data: bytes):
        for line in data.decode("utf-8", errors="replace").splitlines():
            self.board.handle(line)

    def read_available(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while not self.board.out and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.board.out:
            return ""
        text = "\r\n".join(self.board.out) + "\r\n"
        self.board.out.clear()
        return text

    def close(self):
        pass


class Session:
    """Line-based serial communication + timestamped transcript."""

    def __init__(self, transport):
        self.t = transport
        self.buf = ""
        self.pending: list[str] = []
        self.transcript: list[tuple[float, str, str]] = []
        self.t0 = time.monotonic()
        self.last_fault: str | None = None
        self.run_dir: Path | None = None   # cam_blink files go here
        self.step_no = 0
        self.cam_last: dict | None = None  # region and levels of the last blink measurement (for steady)

    def _pump(self, timeout: float):
        data = self.t.read_available(timeout)
        if not data:
            return
        self.buf += data
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                continue
            self.pending.append(line)
            self.transcript.append((time.monotonic() - self.t0, "<", line))
            if line.startswith("HARDFAULT"):
                self.last_fault = line

    def recent(self, n: int = 12) -> str:
        return "\n".join(f"{d} {txt}" for _, d, txt in self.transcript[-n:]) or "(none)"

    def note(self, text: str):
        self.transcript.append((time.monotonic() - self.t0, "#", text))

    def drain(self):
        """Drop old pending lines (so they do not mix with the reply to a new command). Does not wait: reads only what has arrived."""
        self._pump(0)
        self.pending.clear()

    def send_line(self, text: str):
        self.transcript.append((time.monotonic() - self.t0, ">", text))
        self.t.write((text + "\r\n").encode("utf-8"))

    def wait_for(self, pattern: str, timeout: float):
        rx = re.compile(pattern)
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            while self.pending:
                line = self.pending.pop(0)
                seen.append(line)
                m = rx.search(line)
                if m:
                    return m, line, seen
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None, seen
            self._pump(min(0.1, remaining))

    def collect(self, seconds: float) -> list[str]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pump(min(0.1, deadline - time.monotonic()))
        lines, self.pending = self.pending, []
        return lines

    def transcript_text(self) -> str:
        return "\n".join(f"{t:8.3f} {d} {txt}" for t, d, txt in self.transcript)

    def close(self):
        self.t.close()


def open_session(cfg: Config, board: SimBoard | None) -> Session:
    if cfg.get("probe", "backend") == "sim":
        return Session(SimTransport(board))
    return Session(SerialTransport(cfg.get("serial", "port"), int(cfg.get("serial", "baud"))))


# ---------------------------------------------------------------------------
# Test plan runner
# ---------------------------------------------------------------------------

CAM_SMALL_W = 320       # LED search and measurement run on frames scaled down to this width
CAM_ROI_SMALL = 5       # side of the LED region (pixels in the small frame)
CAM_MIN_CONTRAST = 30   # minimum brightness difference to count as blinking (0-255)
ACTIONS = {"flash", "reset", "expect", "send", "silence", "delay", "clock_check", "cam_blink"}
TICK_PATTERN = r"^TICK (\d+)$"
NEVER_MATCH = "__THIS_LINE_MUST_NOT_MATCH__"


def load_plan(cfg: Config, name_or_path: str) -> tuple[Path, dict]:
    p = Path(name_or_path)
    if not p.suffix:
        p = cfg.plans_dir / f"{name_or_path}.toml"
    elif not p.is_absolute():
        p = (Path.cwd() / p) if (Path.cwd() / p).exists() else cfg.resolve(p)
    p = p.resolve()
    if cfg.get("permissions", "plans_dir_only") and cfg.plans_dir.resolve() not in p.parents:
        raise BenchError("permission_denied", f"Only plans inside {cfg.plans_dir} may be run.",
                         ["A human must approve the new plan and put it in the plans/ folder"])
    if not p.is_file():
        raise BenchError("plan_not_found", f"Plan not found: {p}", [f"See the plans in {cfg.plans_dir}"])
    try:
        plan = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise BenchError("plan_invalid", f"Could not read {p.name}: {e}")
    steps = plan.get("step", [])
    if not steps:
        raise BenchError("plan_invalid", f"{p.name} has no [[step]].")
    for i, s in enumerate(steps, 1):
        if s.get("action") not in ACTIONS:
            raise BenchError("plan_invalid", f"{p.name} step {i}: unknown action '{s.get('action')}'",
                             [f"Valid actions: {', '.join(sorted(ACTIONS))}"])
        if s["action"] == "clock_check":
            rng = s.get("range")
            if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(v, (int, float)) for v in rng)):
                raise BenchError("plan_invalid", f"{p.name} step {i}: clock_check needs range = [low, high].")
            if not isinstance(s.get("seconds"), (int, float)) or s["seconds"] <= 0:
                raise BenchError("plan_invalid", f"{p.name} step {i}: clock_check needs seconds > 0.")
        if s["action"] == "cam_blink":
            steady = s.get("steady")
            if steady not in (None, "on", "off"):
                raise BenchError("plan_invalid", f"{p.name} step {i}: cam_blink steady must be 'on' or 'off'.")
            for key in ("range", "duty_range"):
                rng = s.get(key)
                if rng is not None and not (isinstance(rng, list) and len(rng) == 2
                                            and all(isinstance(v, (int, float)) for v in rng)):
                    raise BenchError("plan_invalid", f"{p.name} step {i}: cam_blink {key} must be [low, high].")
            if steady is None and s.get("range") is None:
                raise BenchError("plan_invalid", f"{p.name} step {i}: cam_blink needs range (Hz) or steady.")
        for key in ("pattern", "expect"):
            if key in s:
                try:
                    re.compile(s[key])
                except re.error as e:
                    raise BenchError("plan_invalid", f"{p.name} step {i}: invalid regex: {e}")
    return p, plan


def check_range(m: re.Match, rng) -> tuple[bool, str]:
    if not rng:
        return True, ""
    if not m.groups():
        return False, "range is given but the pattern has no capture group ()"
    try:
        val = float(m.group(1))
    except (TypeError, ValueError):
        return False, f"captured value is not a number: {m.group(1)!r}"
    lo, hi = rng
    if lo <= val <= hi:
        return True, f"value {val:g} within [{lo}, {hi}]"
    return False, f"value {val:g} outside the expected range [{lo}, {hi}]"


def addr2line(elf: Path, addrs: list[int]) -> dict[int, str]:
    """Turns addresses into 'function() file:line' text (if addr2line is available). Unresolved ones are left out of the dict."""
    if not addrs or not elf.is_file():
        return {}
    gcc = find_tool(None, ["arm-none-eabi-gcc"], [
        r"C:\ST\STM32CubeCLT*\GNU-tools-for-STM32\bin\arm-none-eabi-gcc.exe",
        r"C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
        r"C:\Program Files\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
        "/opt/st/stm32cubeclt*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc"])
    if not gcc:
        return {}
    a2l = str(Path(gcc).with_name(Path(gcc).name.replace("gcc", "addr2line")))
    try:
        p = subprocess.run([a2l, "-f", "-C", "-e", str(elf), *(f"0x{a:08x}" for a in addrs)],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    parts = p.stdout.strip().splitlines()
    out = {}
    for a, fn, loc in zip(addrs, parts[0::2], parts[1::2]):
        if "??" not in fn:
            out[a] = f"{fn}() {Path(loc.split(' ')[0]).name}"
    return out


def resolve_fault(elf: Path, fault_line: str) -> str | None:
    """Turns the pc address in a HARDFAULT line into function:file:line (if addr2line is available)."""
    m = re.search(r"pc=0x([0-9A-Fa-f]{8})", fault_line)
    if not m:
        return None
    pc = int(m.group(1), 16)
    return addr2line(elf, [pc]).get(pc)


def check_permission(cfg: Config, key: str, what: str):
    if not cfg.get("permissions", key):
        raise BenchError("permission_denied", f"{what} permission is off (bench.toml [permissions] {key}).",
                         ["Only a human can turn this permission on"])


def run_step(cfg: Config, prog: Programmer, sess: Session, plan: dict, step: dict) -> str:
    a = step["action"]
    timeout = float(step.get("timeout", 2))
    if a == "flash":
        check_permission(cfg, "allow_flash", "Flash")
        image = step.get("image") or plan.get("firmware")
        if not image:
            raise BenchError("plan_invalid", "The flash step needs 'image', or 'firmware' in the plan.")
        sess.drain()
        sess.last_fault = None
        sess.note(f"flash {image}")
        return prog.flash(cfg.resolve(image), under_reset=bool(step.get("under_reset")))
    if a == "reset":
        check_permission(cfg, "allow_reset", "Reset")
        sess.drain()
        sess.last_fault = None
        sess.note("reset")
        return prog.reset()
    if a == "delay":
        time.sleep(float(step.get("seconds", 0.5)))
        return f"Waited {step.get('seconds', 0.5)} s."
    if a == "silence":
        secs = float(step.get("seconds", 1))
        lines = sess.collect(secs)
        if lines:
            raise BenchError("unexpected_output", f"Expected {secs:g} s of silence but got {len(lines)} lines.",
                             ["The board is still running; the expected crash/sleep did not happen"], "\n".join(lines[:10]))
        return f"No output for {secs:g} s (as expected)."

    if a == "clock_check":
        return clock_check(cfg, sess, plan, step, timeout)
    if a == "cam_blink":
        return cam_blink(cfg, prog, sess, step)

    pattern = step.get("pattern") if a == "expect" else step.get("expect")
    if a == "send":
        sess.drain()
        sess.send_line(step["text"])
        if not pattern:
            return f"Sent '{step['text']}'."
    m, line = expect_line(cfg, sess, plan, step, pattern, timeout,
                          f"to '{step['text']}': " if a == "send" else "")
    good, msg = check_range(m, step.get("range"))
    if not good:
        raise BenchError("value_out_of_range", f"'{line}': {msg}", ["Check the measured value and the expected range"])
    return f"'{line}'" + (f" ({msg})" if msg else "")


def expect_line(cfg: Config, sess: Session, plan: dict, step: dict, pattern: str, timeout: float,
                where: str = "") -> tuple[re.Match, str]:
    """Waits for a line that matches the pattern; raises a crash/timeout error if none arrives."""
    m, line, seen = sess.wait_for(pattern, timeout)
    if m:
        return m, line
    if sess.last_fault:
        image = step.get("image") or plan.get("firmware")
        loc = resolve_fault(cfg.resolve(image), sess.last_fault) if image else None
        raise BenchError("target_crashed",
                         f"{where}no reply; the board crashed: {sess.last_fault}",
                         ["The firmware hit a HardFault; look at the code at the pc address"] +
                         ([f"Crash location: {loc}"] if loc else
                          ["Map the pc to code: arm-none-eabi-addr2line -f -e firmware.elf <pc>"]),
                         "Recent serial traffic:\n" + sess.recent())
    causes = ["The firmware does not know this command, or the reply format is different",
              "The board is hung (infinite loop) or crashed",
              "Wrong baud rate (garbled characters)",
              "TX/RX wires swapped, or GND not connected"] if not seen else \
             ["A reply came but did not match the expected pattern: see the lines below"]
    raise BenchError("expect_timeout",
                     f"{where}no line matching /{pattern}/ within {timeout:g} s.",
                     causes, "Recent serial traffic:\n" + sess.recent())


def read_tick(cfg: Config, sess: Session, plan: dict, step: dict, timeout: float) -> tuple[int, float, float]:
    """Sends TICK. Returns (board ms, PC time, round trip s).
    The PC time is the midpoint between sending and the reply: the board counter was read somewhere in that interval."""
    sess.drain()
    t_send = time.monotonic()
    sess.send_line("TICK")
    m, _ = expect_line(cfg, sess, plan, step, TICK_PATTERN, timeout, "to 'TICK': ")
    t_recv = time.monotonic()
    return int(m.group(1)), (t_send + t_recv) / 2, t_recv - t_send


def clock_check(cfg: Config, sess: Session, plan: dict, step: dict, timeout: float) -> str:
    """Compares the board's ms counter with the PC monotonic clock: ratio = board time / PC time."""
    secs = float(step["seconds"])
    lo, hi = step["range"]
    tick0, pc0, rtt0 = read_tick(cfg, sess, plan, step, timeout)
    sess.note(f"clock_check: waiting {secs:g} s")
    time.sleep(max(0.0, pc0 + secs - time.monotonic()))
    tick1, pc1, rtt1 = read_tick(cfg, sess, plan, step, timeout)
    card_ms = tick1 - tick0
    pc_ms = (pc1 - pc0) * 1000
    # uncertainty of the PC time: half the round trip of each reading
    unc_ms = (rtt0 + rtt1) / 2 * 1000
    times = f"board {card_ms} ms, PC {pc_ms:.1f} ms (±{unc_ms:.1f} ms)"
    if card_ms <= 0:
        raise BenchError("target_reset", f"TICK did not advance ({tick0} -> {tick1}); {times}.",
                         ["The board reset during the measurement (look for a BOOT line in the serial log)",
                          "The SysTick interrupt is not running"])
    ratio = card_ms / pc_ms
    msg = f"ratio {ratio:.4f}: {times}"
    if not lo <= ratio <= hi:
        raise BenchError("value_out_of_range", f"clock ratio {ratio:.4f} outside the expected range [{lo}, {hi}]; {times}.",
                         ["Wrong SysTick reload value (should be HSI_HZ / 1000 - 1)",
                          "The clock source or frequency is not what the firmware assumes (HSI 16 MHz, no PLL)",
                          "Bad HSI calibration (RCC_CR HSITRIM)",
                          "Interrupts stay disabled for long periods and SysTick interrupts are missed"])
    return f"{msg} (within [{lo}, {hi}])"


# ---------------------------------------------------------------------------
# LED blink measurement with the camera (cam_blink)
# ---------------------------------------------------------------------------

def record_video(cfg: Config, prog: Programmer, seconds: float, dest: Path) -> None:
    """Records the video and moves it to dest. On a real board uses the camera/ tool, in sim a synthetic ffmpeg video."""
    if isinstance(prog, SimProgrammer):
        prog.board.write_video(dest, seconds)
        return
    tool_dir = cfg.resolve(cfg.get("camera", "tool_dir"))
    bun = str(cfg.resolve(cfg.get("camera", "bun")))
    if not Path(bun).is_file():
        bun = shutil.which("bun") or bun
    cmd = [bun, "scripts/record-video.ts", cfg.get("camera", "serial") or "",
           cfg.get("camera", "camera"), f"{seconds:g}", str(cfg.get("camera", "fps"))]
    # if serial is empty the camera tool picks the only connected device; with more than one device it fails
    try:
        p = subprocess.run(cmd, cwd=tool_dir, capture_output=True, text=True, errors="replace",
                           timeout=seconds + 90)
    except FileNotFoundError:
        raise BenchError("tool_not_found", f"Could not run the camera tool: {bun} / {tool_dir}",
                         ["Check the tool_dir and bun paths in bench.toml [camera]"])
    except subprocess.TimeoutExpired:
        raise BenchError("camera_failed", "Camera recording timed out.",
                         ["The phone is locked or scrcpy is stuck", "Check the phone with adb devices"])
    path = Path(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
    if p.returncode != 0 or not path or path.suffix != ".mp4" or not path.is_file():
        raise BenchError("camera_failed", "Could not record the camera video.",
                         ["The phone is not connected or USB debugging is not authorized (adb devices)",
                          "More than one adb device: set serial in bench.toml [camera]",
                          "The phone screen is locked", f"The camera does not support {cfg.get('camera', 'fps')} fps"],
                         tail(p.stderr + p.stdout))
    shutil.move(str(path), dest)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def video_frames(video: Path):
    """(frames, timestamps in s). The times come from the video's own frame timestamps via ffprobe."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise BenchError("tool_not_found", "cam_blink needs OpenCV and NumPy.",
                         ["sudo apt install python3-opencv python3-numpy"])
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "frame=best_effort_timestamp_time,pts_time", "-of", "json", str(video)],
                       capture_output=True, text=True, timeout=60)
    try:
        ts = [float(f.get("best_effort_timestamp_time") or f["pts_time"])
              for f in json.loads(p.stdout)["frames"]]
    except (ValueError, KeyError, TypeError):
        raise BenchError("camera_failed", "Could not read the video frame timestamps (ffprobe).", [], tail(p.stderr))
    cap = cv2.VideoCapture(str(video))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    if len(frames) != len(ts) or len(frames) < 10:
        raise BenchError("camera_failed",
                         f"Video frames and timestamps do not match ({len(frames)} frames, {len(ts)} timestamps).",
                         ["The video is corrupt or too short"])
    return frames, np.array(ts) - ts[0]


def find_led_roi(small, scale: float) -> tuple[list[int], list[dict]]:
    """Finds the region whose brightness changes most across the frames. Returns [x, y, w, h] at full
    resolution and the top 3 candidates (to reveal other blinking spots such as reflections)."""
    import cv2
    import numpy as np
    k = CAM_ROI_SMALL
    std = cv2.blur(small.std(axis=0), (k, k))
    cands = []
    for _ in range(3):
        y, x = np.unravel_index(int(np.argmax(std)), std.shape)
        cands.append({"x": round(x / scale), "y": round(y / scale), "std": round(float(std[y, x]), 1)})
        std[max(0, y - 2 * k):y + 2 * k, max(0, x - 2 * k):x + 2 * k] = 0
    cx, cy = cands[0]["x"], cands[0]["y"]
    side = round(k / scale)
    return [cx - side // 2, cy - side // 2, side, side], cands


def blink_edges(t, sig, thr: float, hyst: float) -> tuple[list[float], list[float]]:
    """Threshold crossings with hysteresis; the crossing time is linearly interpolated between two frames."""
    rising, falling = [], []
    state = None
    for i, v in enumerate(sig):
        if state is None:
            state = v > thr
            continue
        new = (v > thr + hyst) if not state else not (v < thr - hyst)
        if new != state:
            a, b = sig[i - 1], v
            frac = (thr - a) / (b - a) if b != a else 0.5
            te = t[i - 1] + min(max(frac, 0.0), 1.0) * (t[i] - t[i - 1])
            (rising if new else falling).append(float(te))
            state = new
    return rising, falling


def edge_freq(edges: list[float]) -> float | None:
    return (len(edges) - 1) / (edges[-1] - edges[0]) if len(edges) >= 2 else None


def cam_blink(cfg: Config, prog: Programmer, sess: Session, step: dict) -> str:
    import cv2
    import numpy as np
    secs = float(step.get("seconds", 4))
    skip = float(step.get("skip_s", 0.5))
    steady = step.get("steady")
    run_dir = sess.run_dir or cfg.runs_dir
    base = f"cam_step{sess.step_no:02d}"
    video = run_dir / f"{base}.mp4"
    sess.note(f"cam_blink: {secs:g} s video -> {video.name}")
    record_video(cfg, prog, secs, video)
    frames, t = video_frames(video)

    h, w = frames[0].shape[:2]
    scale = CAM_SMALL_W / w
    # Color-independent brightness: the max of the channels (green, red and blue LEDs look the same)
    small = np.stack([cv2.resize(f.max(axis=2), (CAM_SMALL_W, round(h * scale)), interpolation=cv2.INTER_AREA)
                      for f in frames]).astype(np.float32)
    use = t >= skip  # skip the first frames while the camera exposure/focus settles
    fixed_roi = cfg.get("camera", "roi")
    cands = []
    if fixed_roi:
        roi, roi_src = [int(v) for v in fixed_roi], "bench.toml"
    elif steady:
        if not sess.cam_last:
            raise BenchError("plan_invalid", "LED region unknown for the steady check.",
                             ["The plan needs a blinking cam_blink step first (it finds the region)",
                              "or set roi = [x, y, w, h] in bench.toml [camera]"])
        roi, roi_src = sess.cam_last["roi"], "previous cam_blink step"
    else:
        roi, cands = find_led_roi(small[use], scale)
        roi_src = "auto"
    x, y, rw, rh = roi
    sx0, sy0 = max(0, int(x * scale)), max(0, int(y * scale))
    sx1, sy1 = max(sx0 + 1, int(round((x + rw) * scale))), max(sy0 + 1, int(round((y + rh) * scale)))
    sig_all = small[:, sy0:sy1, sx0:sx1].mean(axis=(1, 2))
    tt, sig = t[use], sig_all[use]
    dts = np.diff(tt)
    fps = 1.0 / float(np.median(dts))
    lo_lvl, hi_lvl = float(np.percentile(sig, 5)), float(np.percentile(sig, 95))
    contrast = hi_lvl - lo_lvl

    np.savetxt(run_dir / f"{base}.csv", np.column_stack([t, sig_all]), fmt="%.4f", delimiter=",",
               header="t_s,brightness", comments="")
    mark = frames[int(np.argmax(np.where(use, sig_all, -1)))].copy()
    cv2.rectangle(mark, (x, y), (x + rw, y + rh), (0, 0, 255), 3)
    for c in cands[1:]:
        cv2.circle(mark, (c["x"], c["y"]), 20, (0, 255, 255), 2)

    def save_mark(label: str):
        cv2.putText(mark, label, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)
        cv2.imwrite(str(run_dir / f"{base}_roi.jpg"), mark)

    where = f"region {roi} ({roi_src}), {len(tt)} frames, {fps:.1f} fps"
    files = f"video {video.name}, marked frame {base}_roi.jpg"

    if steady:
        prev = sess.cam_last
        thr = prev["threshold"] if prev and roi == prev["roi"] else None
        if thr is None:
            raise BenchError("plan_invalid", "On/off threshold unknown for the steady check.",
                             ["A blinking cam_blink step on the same region must come first"])
        hyst = 0.1 * prev["contrast"]
        rising, falling = blink_edges(tt, sig, thr, hyst)
        level = float(np.median(sig))
        is_on = level > thr
        save_mark(f"steady {'ON' if is_on else 'OFF'}? level {level:.0f} thr {thr:.0f}")
        msg = (f"level {level:.0f} (threshold {thr:.0f}, previous on/off {prev['hi']:.0f}/{prev['lo']:.0f}), "
               f"{len(rising) + len(falling)} transitions; {where}; {files}")
        if rising or falling:
            raise BenchError("value_out_of_range", f"LED should be steady but has {len(rising) + len(falling)} transitions: {msg}",
                             ["The LED is still blinking (heartbeat not stopped)", "Camera shake or a lighting change"])
        if is_on != (steady == "on"):
            raise BenchError("value_out_of_range",
                             f"LED should be steady {'ON' if steady == 'on' else 'OFF'}, "
                             f"measured {'on' if is_on else 'off'}: {msg}",
                             ["The LED command was not applied", "Wrong LED region (see the marked frame)"])
        return f"LED steady {'on' if is_on else 'off'}: {msg}"

    if contrast < CAM_MIN_CONTRAST:
        save_mark(f"no blink, contrast {contrast:.0f}")
        raise BenchError("no_blink_detected",
                         f"No blinking in the LED region (contrast {contrast:.0f} < {CAM_MIN_CONTRAST}); {where}; {files}.",
                         ["The LED is not blinking (board hung or LED steady)", "The phone is not facing the board (see the marked frame)",
                          "Wrong LED region: pin it with roi in bench.toml [camera]"])
    thr = (lo_lvl + hi_lvl) / 2
    rising, falling = blink_edges(tt, sig, thr, 0.1 * contrast)
    fr, ff = edge_freq(rising), edge_freq(falling)
    if fr is None and ff is None:
        save_mark(f"edges {len(rising)}/{len(falling)}")
        raise BenchError("no_blink_detected",
                         f"Not enough transitions for a frequency (rising {len(rising)}, falling {len(falling)}); {where}; {files}.",
                         ["Recording too short: increase seconds", "The LED blinks too slowly"])
    # Average of the two edge types, weighted by their period counts
    n_r, n_f = max(len(rising) - 1, 0), max(len(falling) - 1, 0)
    freq = ((fr or 0) * n_r + (ff or 0) * n_f) / (n_r + n_f)
    # Duty: on time from each rising edge to the next falling edge / period
    ons = [next(f for f in falling if f > r) - r for r in rising if any(f > r for f in falling)]
    duty = float(np.mean(ons)) * freq if ons else None
    sess.cam_last = {"roi": roi, "threshold": thr, "contrast": contrast, "hi": hi_lvl, "lo": lo_lvl}
    save_mark(f"{freq:.3f} Hz duty {duty:.2f}" if duty is not None else f"{freq:.3f} Hz")

    lo, hi = step["range"]
    duty_txt = f"duty {duty:.2f}" if duty is not None else "duty not measurable"
    msg = (f"{freq:.3f} Hz, {duty_txt} (rising {len(rising)}, falling {len(falling)} edges, "
           f"contrast {contrast:.0f}); {where}; {files}")
    if cands:
        msg += "; candidates " + ", ".join(f"({c['x']},{c['y']}) std {c['std']}" for c in cands)
    warn = ""
    if freq > fps / 4:
        warn = (f"WARNING: frequency above a quarter of the frame rate ({fps / 4:.1f} Hz); "
                f"{fps / freq:.1f} frames per period, measurement unreliable. ")
    if not lo <= freq <= hi:
        raise BenchError("value_out_of_range", f"{warn}frequency {freq:.3f} Hz outside the expected range [{lo}, {hi}]: {msg}",
                         ["The firmware's blink rate or clock is wrong",
                          "Measuring the wrong region: see the marked frame; if needed set roi in bench.toml [camera]",
                          "Frame rate too low (sampling error)"])
    drng = step.get("duty_range")
    if drng and (duty is None or not drng[0] <= duty <= drng[1]):
        raise BenchError("value_out_of_range", f"{warn}{duty_txt}, expected [{drng[0]}, {drng[1]}]: {msg}",
                         ["The LED on/off times differ in the firmware", "The camera exposure time lengthens the on time"])
    return f"{warn}{msg} (within [{lo}, {hi}] Hz)"


def recover_board(cfg: Config, prog: Programmer, sess: Session) -> dict:
    banner = cfg.get("recovery", "banner")
    bt = float(cfg.get("recovery", "banner_timeout_s"))
    actions = []
    try:
        sess.drain()
        sess.note("recovery: reset")
        prog.reset()
        m, line, _ = sess.wait_for(banner, bt)
        actions.append({"action": "reset", "ok": bool(m), "line": line})
        if m:
            return {"attempted": True, "ok": True, "actions": actions}
    except BenchError as e:
        actions.append({"action": "reset", "ok": False, "error": e.summary})

    golden = cfg.resolve(cfg.get("recovery", "golden_image"))
    try:
        sess.drain()
        sess.note(f"recovery: flash golden {golden.name} under reset")
        prog.flash(golden, under_reset=True)
        m, line, _ = sess.wait_for(banner, bt)
        actions.append({"action": "flash_golden_under_reset", "ok": bool(m), "line": line})
        return {"attempted": True, "ok": bool(m), "actions": actions}
    except BenchError as e:
        actions.append({"action": "flash_golden_under_reset", "ok": False, "error": e.summary})
        return {"attempted": True, "ok": False, "actions": actions}


def mutate_plan(plan: dict) -> tuple[dict, int | None]:
    """Makes the last 'expectation' step impossible to match (negative check)."""
    plan = json.loads(json.dumps(plan))
    for i in range(len(plan["step"]) - 1, -1, -1):
        s = plan["step"][i]
        if s["action"] == "clock_check":
            s["range"] = [-2.0, -1.0]  # the ratio can never be negative
            s["seconds"] = min(float(s["seconds"]), 1.0)
            return plan, i + 1
        if s["action"] == "cam_blink":
            if s.get("steady"):
                s["steady"] = "off" if s["steady"] == "on" else "on"  # expect the opposite
            else:
                s["range"] = [-2.0, -1.0]  # the frequency can never be negative
            return plan, i + 1
        key = "pattern" if s["action"] == "expect" else ("expect" if s["action"] == "send" and s.get("expect") else None)
        if key:
            s[key] = s[key] + NEVER_MATCH
            s["timeout"] = min(float(s.get("timeout", 2)), 1.0)
            return plan, i + 1
    return plan, None


STLINK_PIDS = {"3744", "3748", "374b", "374e", "374f", "3752", "3753", "3754", "3757"}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_serial(cfg: Config) -> str:
    """ST-Link serial number: from bench.toml, otherwise from sysfs on Linux."""
    if cfg.get("probe", "backend") == "sim":
        return "sim"
    if cfg.get("probe", "serial_number"):
        return str(cfg.get("probe", "serial_number"))
    if IS_WIN or not Path("/sys/bus/usb/devices").exists():
        return "unknown (set serial_number in bench.toml [probe])"
    found = []
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            if (d / "idVendor").read_text().strip() != "0483" or \
                    (d / "idProduct").read_text().strip() not in STLINK_PIDS:
                continue
            raw = (d / "serial").read_text(encoding="utf-8", errors="replace").strip("\n")
        except OSError:
            continue
        # ST-Link V2 reports its serial number as raw bytes; write it as hex like OpenOCD does
        if not raw.isalnum() and all(ord(c) < 256 for c in raw):
            raw = "".join(f"{ord(c):02X}" for c in raw)
        found.append(raw)
    if not found:
        return "not found"
    return ", ".join(sorted(found)) + (" (more than one ST-Link connected)" if len(found) > 1 else "")


def plan_images(cfg: Config, plan: dict) -> list[Path]:
    imgs = []
    for s in plan["step"]:
        if s.get("action") == "flash" and (img := s.get("image") or plan.get("firmware")):
            p = cfg.resolve(img)
            if p not in imgs:
                imgs.append(p)
    return imgs


def run_metadata_start(cfg: Config, plan: dict, prog: Programmer) -> dict:
    fw = []
    for p in plan_images(cfg, plan):
        fw.append({"path": str(p), "sha256": file_sha256(p) if p.is_file() else None})
    return {
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "bench_version": VERSION,
        "bench_sha256": file_sha256(Path(__file__).resolve()),
        "programmer": prog.name,
        "programmer_version": prog.tool_version(),
        "probe_serial": probe_serial(cfg),
        "firmware": fw,
    }


def read_uid_for_report(prog: Programmer, sess: Session) -> dict:
    """From the debug port first (mem 0x1FFF7A10), otherwise from the UID reply on the serial port."""
    seen = next((txt.split()[1] for _, d, txt in reversed(sess.transcript)
                 if d == "<" and re.fullmatch(r"UID [0-9A-F]{24}", txt)), None)
    try:
        uid = prog.read_uid()
        src = f"debug port, mem 0x{UID_ADDR:08X}"
        if seen:
            src += ", same as the UID on the serial port" if seen == uid else f", WARNING: serial port differs ({seen})"
        return {"uid": uid, "source": src}
    except BenchError as e:
        if seen:
            return {"uid": seen, "source": "serial port UID command"}
        return {"uid": None, "source": f"unreadable: {e.summary}"}


def execute_plan(cfg: Config, plan_path: Path, plan: dict, board, label: str = "",
                 operator: str | None = None) -> dict:
    name = plan.get("name", plan_path.stem)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.runs_dir / f"{stamp}_{name}{label}"
    n = 2
    while run_dir.exists():
        run_dir = cfg.runs_dir / f"{stamp}_{name}{label}-{n}"
        n += 1
    run_dir.mkdir(parents=True)

    prog = make_programmer(cfg, board)
    meta = run_metadata_start(cfg, plan, prog)
    if operator:
        meta["operator"] = operator
    sess = open_session(cfg, board)
    sess.run_dir = run_dir
    results = []
    failed: BenchError | None = None
    t_start = time.monotonic()
    try:
        for i, step in enumerate(plan["step"], 1):
            t0 = time.monotonic()
            desc = step.get("name") or step["action"] + (f" {step['text']}" if step.get("text") else "")
            sess.step_no = i
            try:
                msg = run_step(cfg, prog, sess, plan, step)
                results.append({"step": i, "name": desc, "ok": True, "summary": msg,
                                "duration_s": round(time.monotonic() - t0, 3)})
            except BenchError as e:
                failed = e
                results.append({"step": i, "name": desc, "ok": False, "error_type": e.error_type,
                                "summary": e.summary, "likely_causes": e.likely_causes,
                                "details": e.details, "duration_s": round(time.monotonic() - t0, 3)})
                break
        recovery = recover_board(cfg, prog, sess) if failed else {"attempted": False}
    finally:
        (run_dir / "serial.log").write_text(sess.transcript_text() + "\n", encoding="utf-8")
        sess.close()

    meta.update(read_uid_for_report(prog, sess))
    meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    ok = failed is None
    result = {
        "ok": ok, "command": "run", "plan": name, "plan_file": str(plan_path),
        "summary": (f"{name}: {len(results)}/{len(plan['step'])} steps passed." if ok else
                    f"{name}: failed at step {results[-1]['step']}: {failed.summary}"),
        "duration_s": round(time.monotonic() - t_start, 2),
        "steps": results, "recovery": recovery, "report_dir": str(run_dir), "meta": meta,
    }
    if failed:
        result.update(error_type=failed.error_type, likely_causes=failed.likely_causes)
        if failed.details:
            result["details"] = failed.details
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report_md(run_dir, result, plan)
    return result


AUDIT_PENDING = "- Audit log hash: (not written yet)"


def write_report_md(run_dir: Path, r: dict, plan: dict) -> None:
    m = r["meta"]
    L = [f"# Test report: {r['plan']}", "",
         f"- Result: **{'PASS' if r['ok'] else 'FAIL'}**",
         f"- Started: {m['started'].replace('T', ' ')}",
         f"- Finished: {m['finished'].replace('T', ' ')}",
         f"- Duration: {r['duration_s']} s",
         f"- Plan file: `{r['plan_file']}`"]
    if m.get("operator"):
        L.insert(3, f"- Operator: {m['operator']}")
    if plan.get("description"):
        L.append(f"- Description: {plan['description']}")
    L += ["", "## Environment", ""]
    if not m["firmware"]:
        L.append("- Firmware: this plan does not flash")
    for fw in m["firmware"]:
        L.append(f"- Firmware: `{fw['path']}`")
        L.append(f"  - SHA-256: `{fw['sha256'] or 'no file'}`")
    L += [f"- Board UID: `{m['uid'] or '-'}` ({m['source']})",
          f"- ST-Link serial no: `{m['probe_serial']}`",
          f"- bench.py: {m['bench_version']} (file SHA-256 `{m['bench_sha256'][:16]}…`)",
          f"- {m['programmer']}: {m['programmer_version']}",
          f"- Host: {m['host']}",
          AUDIT_PENDING]
    L += ["", "| # | Step | Result | Summary | Duration (s) |", "|---|---|---|---|---|"]
    for s in r["steps"]:
        summ = str(s["summary"]).replace("|", "\\|")
        L.append(f"| {s['step']} | {s['name']} | {'PASS' if s['ok'] else 'FAIL'} | {summ} | {s['duration_s']} |")
    if not r["ok"]:
        L += ["", "## Error", "", f"- Type: `{r.get('error_type')}`"]
        for c in r.get("likely_causes", []):
            L.append(f"- Likely cause: {c}")
        if r.get("details"):
            L += ["", "```", str(r["details"]), "```"]
    rec = r.get("recovery", {})
    if rec.get("attempted"):
        L += ["", "## Recovery", "", f"- Result: {'board recovered' if rec['ok'] else 'NOT RECOVERED, a human must step in'}"]
        for a in rec["actions"]:
            L.append(f"- {a['action']}: {'OK' if a['ok'] else 'failed'} {a.get('line') or a.get('error') or ''}")
    L += ["", "Serial transcript: `serial.log`"]
    (run_dir / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(cfg, args, board) -> dict:
    plan_path, plan = load_plan(cfg, args.plan)
    r1 = execute_plan(cfg, plan_path, plan, board)
    if not args.verify_can_fail or not r1["ok"]:
        return r1
    mplan, mstep = mutate_plan(plan)
    if mstep is None:
        r1["negative_check"] = {"ok": False, "summary": "The plan has no expectation step; the negative check could not be run."}
        r1["ok"] = False
        r1["error_type"] = "test_cannot_fail"
        r1["summary"] += " But the plan has no expectations, so this test proves nothing."
        return r1
    if board is not None:
        board.out.clear()
    r2 = execute_plan(cfg, plan_path, mplan, board, label="_negative")
    neg = {"mutated_step": mstep, "report_dir": r2["report_dir"], "failed_as_expected": not r2["ok"],
           "recovery_ok": r2["recovery"].get("ok")}
    r1["negative_check"] = neg
    if r2["ok"]:
        r1.update(ok=False, error_type="test_cannot_fail",
                  summary=f"{r1['plan']}: PASSED even with a deliberately broken expectation. This test measures nothing.",
                  likely_causes=["The bench does not report a failed expectation as FAIL (bench.py bug)",
                                 "The broken step never runs"])
    else:
        r1["summary"] += f" Negative check: with step {mstep} broken it FAILED as expected."
    return r1


def cmd_flash(cfg, args, board) -> dict:
    check_permission(cfg, "allow_flash", "Flash")
    image = Path(args.image)
    if not image.is_absolute() and not image.exists():
        image = cfg.resolve(image)  # as in plans: if not found, relative to bench.toml's folder
    if not image.is_file():
        raise BenchError("image_not_found", f"{image} not found.", ["Build it first: python firmware/build.py"])
    msg = make_programmer(cfg, board).flash(image, under_reset=args.under_reset)
    return ok_result("flash", msg, image=str(image.resolve()))


def cmd_reset(cfg, args, board) -> dict:
    check_permission(cfg, "allow_reset", "Reset")
    prog = make_programmer(cfg, board)
    if not args.expect:
        return ok_result("reset", prog.reset())
    sess = open_session(cfg, board)
    try:
        sess.drain()
        prog.reset()
        m, line, seen = sess.wait_for(args.expect, args.timeout)
    finally:
        sess.close()
    if not m:
        raise BenchError("expect_timeout", f"No /{args.expect}/ within {args.timeout:g} s after reset.",
                         ["The firmware crashes at boot", "Serial connection/baud problem"],
                         "Received lines:\n" + ("\n".join(seen) or "(none)"))
    return ok_result("reset", f"Board reset, boot line: {line}", lines=seen)


def cmd_send(cfg, args, board) -> dict:
    sess = open_session(cfg, board)
    try:
        sess.drain()
        sess.send_line(args.text)
        if args.expect:
            m, line, seen = sess.wait_for(args.expect, args.timeout)
            if not m:
                if sess.last_fault:
                    raise BenchError("target_crashed", f"The board crashed after '{args.text}': {sess.last_fault}",
                                     ["Map the pc to code: arm-none-eabi-addr2line -f -e firmware.elf <pc>",
                                      "Bring the board back: bench.py reset --expect \"^BOOT\""], sess.recent())
                raise BenchError("expect_timeout",
                                 f"No /{args.expect}/ reply to '{args.text}' within {args.timeout:g} s.",
                                 ["The board may have crashed or hung earlier: try 'bench.py reset --expect \"^BOOT\"'",
                                  "TX/RX wires swapped, or GND not connected"],
                                 "Recent serial traffic:\n" + sess.recent())
            good, msg = check_range(m, [args.min, args.max] if args.min is not None else None)
            if not good:
                raise BenchError("value_out_of_range", f"'{line}': {msg}")
            return ok_result("send", f"'{args.text}' -> '{line}'", response=seen)
        lines = sess.collect(args.timeout)
        return ok_result("send", f"Sent '{args.text}', {len(lines)} reply lines.", response=lines)
    finally:
        sess.close()


def cmd_read(cfg, args, board) -> dict:
    sess = open_session(cfg, board)
    try:
        if args.until:
            m, line, seen = sess.wait_for(args.until, args.seconds)
            if not m:
                raise BenchError("expect_timeout", f"No /{args.until}/ within {args.seconds:g} s.",
                                 [], "\n".join(seen) or "(none)")
            return ok_result("read", f"Found: {line}", lines=seen)
        lines = sess.collect(args.seconds)
        return ok_result("read", f"Read {len(lines)} lines in {args.seconds:g} s.", lines=lines)
    finally:
        sess.close()


def cmd_recover(cfg, args, board) -> dict:
    check_permission(cfg, "allow_flash", "Flash")
    golden = cfg.resolve(cfg.get("recovery", "golden_image"))
    if not golden.is_file():
        raise BenchError("image_not_found", f"Golden firmware not found: {golden}",
                         ["Build it with python firmware/build.py", "Check the golden_image path in bench.toml [recovery]"])
    prog = make_programmer(cfg, board)
    sess = open_session(cfg, board)
    try:
        rec = recover_board(cfg, prog, sess)
    finally:
        sess.close()
    if not rec["ok"]:
        raise BenchError("recovery_failed", "The board could not be recovered automatically.",
                         ["Unplug and replug the board's USB cable, then try again",
                          "Set BOOT0 to 1, reset the board, try again (system bootloader)",
                          "A human must step in"], json.dumps(rec, ensure_ascii=False, indent=2))
    return ok_result("recover", "Board recovered and the boot message was seen.", recovery=rec)


# --- Debugging: read-only (halt-dump, mem) and gdbserver for humans ---

# Regions allowed to be read on the STM32F407. Option bytes (0x1FFFC000) are deliberately excluded.
READ_REGIONS = [
    (0x08000000, 0x08100000, "flash"),
    (0x10000000, 0x10010000, "CCM RAM"),
    (0x1FFF0000, 0x1FFF7A30, "system memory, OTP, UID"),
    (0x20000000, 0x20020000, "SRAM"),
    (0x40000000, 0x60000000, "peripherals"),
    (0xE0000000, 0xE0100000, "Cortex-M core (SCB, NVIC, SysTick)"),
]
MEM_MAX_WORDS = 256
UID_ADDR = 0x1FFF7A10

SCB_REGS = ["cfsr", "hfsr", "dfsr", "mmfar", "bfar"]  # consecutive from 0xE000ED28
CFSR_BITS = {
    0: "IACCVIOL: instruction fetch from a forbidden address (MPU)",
    1: "DACCVIOL: data access to a forbidden address (MPU)",
    3: "MUNSTKERR: stack read error on exception return (MPU)",
    4: "MSTKERR: stack write error on exception entry (MPU)",
    5: "MLSPERR: error while saving FPU state (MPU)",
    7: "MMARVALID: MMFAR is valid",
    8: "IBUSERR: bus error on instruction fetch",
    9: "PRECISERR: bus error on data access, address in BFAR",
    10: "IMPRECISERR: imprecise bus error (pc may point past the faulting instruction)",
    11: "UNSTKERR: bus error reading the stack on exception return",
    12: "STKERR: stack overflow / bus error on exception entry",
    13: "LSPERR: bus error while saving FPU state",
    15: "BFARVALID: BFAR is valid",
    16: "UNDEFINSTR: undefined instruction (udf, corrupt code, jump to a wrong address)",
    17: "INVSTATE: invalid state (jump to an address with the Thumb bit 0, e.g. a NULL function pointer)",
    18: "INVPC: return with an invalid EXC_RETURN",
    19: "NOCP: coprocessor disabled (float used before enabling the FPU)",
    24: "UNALIGNED: unaligned access",
    25: "DIVBYZERO: division by zero",
}
HFSR_BITS = {1: "VECTTBL: could not read the vector table",
             30: "FORCED: a configurable fault was escalated to HardFault (see CFSR)",
             31: "DEBUGEVT: debug event"}
EXC_NAMES = {0: "thread (normal code)", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
             6: "UsageFault", 11: "SVCall", 14: "PendSV", 15: "SysTick"}


def parse_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise BenchError("bad_argument", f"Not a number: {text!r}", ["Write hex with 0x, or decimal"])


def is_code_addr(v: int) -> bool:
    return 0x08000000 <= v < 0x08100000


def parse_mdw(out: str) -> list[tuple[int, int]]:
    """Turns OpenOCD 'mdw' output into a list of (address, value)."""
    words = []
    for m in re.finditer(r"^0x([0-9a-fA-F]+):((?: [0-9a-fA-F]{8})+)", out, re.M):
        base = int(m.group(1), 16)
        for i, w in enumerate(m.group(2).split()):
            words.append((base + 4 * i, int(w, 16)))
    return words


def parse_regs(out: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"^\(\d+\) (\w+) \(/\d+\): (0x[0-9a-fA-F]+)", out, re.M)}


def decode_bits(value: int, table: dict[int, str]) -> list[str]:
    return [desc for bit, desc in table.items() if value & (1 << bit)]


def symbols_elf(cfg: Config, args) -> Path:
    return Path(args.elf).resolve() if args.elf else cfg.resolve(cfg.get("recovery", "golden_image"))


def cmd_halt_dump(cfg, args, board) -> dict:
    """Halts the core, reads the registers, fault registers and stack, then resumes."""
    prog = make_programmer(cfg, board)
    n_stack = 48
    out = prog.debug_commands([
        "halt",
        "echo @@REGS", "reg",
        "echo @@SCB", "mdw 0xE000ED28 5",
        # read without going past the end of SRAM (0x20020000); if sp is bad let it fail but continue with the rest
        "echo @@STACK",
        "set _sp [lindex [regexp -inline {0x[0-9a-fA-F]+} [reg sp]] 0]; "
        f"set _n {n_stack}; "
        "if {$_sp >= 0x20000000 && $_sp < 0x20020000 && (0x20020000 - $_sp) / 4 < $_n} "
        "{ set _n [expr {(0x20020000 - $_sp) / 4}] }; "
        # output inside catch is not printed but returned as the result; we print it with echo
        "if {$_n > 0 && [catch { mdw $_sp $_n } _r] == 0} { echo $_r }",
        *([] if args.keep_halted else ["resume"]),
    ])
    sections = dict(re.findall(r"@@(\w+)\n(.*?)(?=@@\w+\n|\Z)", out, re.S))
    regs = parse_regs(sections.get("REGS", ""))
    if "pc" not in regs:
        raise BenchError("tool_error", "Could not read the registers.", ["The core may not have halted"], tail(out))
    scb = dict(zip(SCB_REGS, (v for _, v in parse_mdw(sections.get("SCB", "")))))
    # The address registers are meaningful only if their VALID bit in CFSR is 1
    if not scb.get("cfsr", 0) & (1 << 7):
        scb.pop("mmfar", None)
    if not scb.get("cfsr", 0) & (1 << 15):
        scb.pop("bfar", None)
    stack = parse_mdw(sections.get("STACK", ""))

    elf = symbols_elf(cfg, args)
    exc = regs.get("xPSR", 0) & 0x1FF

    # If inside an exception, look for EXC_RETURN (0xFFFFFFxx) on the stack to find the hardware-stacked frame:
    # r0 r1 r2 r3 r12 lr pc xpsr follow right after it (if the handler pushed LR last).
    frame = None
    if exc:
        for i, (_, v) in enumerate(stack):
            if v & 0xFFFFFFE0 == 0xFFFFFFE0 and v & 0x4 == 0 and i + 8 < len(stack):
                f = [w for _, w in stack[i + 1:i + 9]]
                if is_code_addr(f[6]):
                    frame = dict(zip(["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"], f))
                    frame["found_at"] = stack[i + 1][0]
                    break

    candidates = [v for _, v in stack if is_code_addr(v) and v & 1]  # Thumb return addresses
    want = [regs["pc"], regs.get("lr", 0) & ~1] + ([frame["pc"], frame["lr"] & ~1] if frame else []) + \
           [(v & ~1) - 2 for v in candidates]  # 2 bytes before the return address = the calling instruction
    syms = addr2line(elf, [a for a in want if is_code_addr(a)])

    def h(v):
        return f"0x{v:08X}"

    result_regs = {k: h(regs[k]) for k in ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9",
                                           "r10", "r11", "r12", "sp", "lr", "pc", "xPSR", "msp", "psp"]
                   if k in regs}
    faults = {k: h(v) for k, v in scb.items()}
    explain = decode_bits(scb.get("cfsr", 0), CFSR_BITS) + decode_bits(scb.get("hfsr", 0), HFSR_BITS)
    chain = []
    for v in candidates:
        loc = syms.get((v & ~1) - 2)
        if loc and (not chain or chain[-1]["at"] != loc):
            chain.append({"value": h(v), "at": loc})

    where = syms.get(regs["pc"], "no symbol")
    exc_name = EXC_NAMES.get(exc, f"IRQ{exc - 16}" if exc >= 16 else f"exception {exc}")
    summary = f"Core in {exc_name}, pc={h(regs['pc'])} ({where})."
    if frame:
        summary += f" At the crash: pc={h(frame['pc'])} ({syms.get(frame['pc'], 'no symbol')})."
    summary += " Core left halted." if args.keep_halted else " Core resumed."

    r = ok_result("halt-dump", summary, exception=exc_name, registers=result_regs,
                  pc_at=syms.get(regs["pc"]), lr_at=syms.get(regs.get("lr", 0) & ~1),
                  fault_registers=faults, fault_explained=explain,
                  stacked_frame=({k: h(v) for k, v in frame.items()} |
                                 {"pc_at": syms.get(frame["pc"]), "lr_at": syms.get(frame["lr"] & ~1)})
                  if frame else None,
                  stack_code_addresses=chain, stack=[f"{h(a)}: {h(v)}" for a, v in stack],
                  symbols=str(elf), resumed=not args.keep_halted)
    if not args.json:
        print(f"  pc  {result_regs['pc']}  {r['pc_at'] or ''}")
        print(f"  lr  {result_regs.get('lr', '?')}  {r['lr_at'] or ''}")
        print(f"  sp  {result_regs.get('sp', '?')}   xPSR {result_regs.get('xPSR', '?')}  ({exc_name})")
        print("  " + "  ".join(f"{k}={v}" for k, v in faults.items()))
        for e in explain:
            print(f"    - {e}")
        if frame:
            print(f"  Stacked frame: pc={h(frame['pc'])} {r['stacked_frame']['pc_at'] or ''}"
                  f"  lr={h(frame['lr'])} {r['stacked_frame']['lr_at'] or ''}")
        if chain:
            print("  Code addresses on the stack (likely call chain, top down):")
            for c in chain:
                print(f"    {c['value']}  {c['at']}")
        print(f"  Symbols: {elf}  (names are wrong if the board runs other firmware)")
    return r


def cmd_mem(cfg, args, board) -> dict:
    addr = parse_int(args.addr)
    count = args.count
    if addr % 4:
        raise BenchError("bad_argument", f"The address must be a multiple of 4: 0x{addr:08X}")
    if not 1 <= count <= MEM_MAX_WORDS:
        raise BenchError("bad_argument", f"--count must be between 1 and {MEM_MAX_WORDS}.")
    end = addr + 4 * count
    region = next((r for r in READ_REGIONS if r[0] <= addr and end <= r[1]), None)
    if not region:
        raise BenchError("permission_denied", f"0x{addr:08X}-0x{end - 1:08X} is outside the allowed regions.",
                         ["Allowed: " + ", ".join(f"{n} 0x{a:08X}-0x{b - 1:08X}" for a, b, n in READ_REGIONS)])
    prog = make_programmer(cfg, board)
    # The core is not halted; ST-Link can read memory while it runs.
    words = parse_mdw(prog.debug_commands([f"mdw 0x{addr:08X} {count}"]))
    if len(words) != count:
        raise BenchError("tool_error", f"Asked for {count} words, read {len(words)}.",
                         ["No memory at this address (bus error); see the memory map in the STM32F407 reference manual"])
    if not args.json:
        for i in range(0, len(words), 4):
            row = words[i:i + 4]
            print(f"  0x{row[0][0]:08X}:  " + " ".join(f"{v:08X}" for _, v in row))
    return ok_result("mem", f"Read {count} words from 0x{addr:08X} ({region[2]}).",
                     words=[{"addr": f"0x{a:08X}", "value": f"0x{v:08X}"} for a, v in words])


def cmd_gdbserver(cfg, args, board) -> dict:
    """For humans: starts the OpenOCD GDB server and keeps the bench locked until Ctrl+C."""
    prog = make_programmer(cfg, board)
    if not hasattr(prog, "gdb_server_cmd"):
        raise BenchError("not_supported", "gdbserver is only available with the openocd backend.")
    elf = symbols_elf(cfg, args)
    print(f"Starting the GDB server on localhost:{args.port}. Press Ctrl+C to stop.")
    print(f"In another terminal:  gdb-multiarch {elf} -ex 'target extended-remote :{args.port}'")
    print("(if gdb-multiarch is missing: sudo apt install gdb-multiarch)\n", flush=True)
    p = subprocess.Popen(prog.gdb_server_cmd(args.port))
    try:
        rc = p.wait()
    except KeyboardInterrupt:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        return ok_result("gdbserver", "GDB server stopped.")
    if rc != 0:
        raise BenchError("tool_error", f"openocd exit code {rc}", ["See the openocd output above"])
    return ok_result("gdbserver", "GDB server exited.")


def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        raise BenchError("pyserial_missing", "pyserial is not installed.",
                         ["Windows: py -m pip install pyserial", "Linux: sudo apt install python3-serial"])
    return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in lp.comports()]


def cmd_ports(cfg, args, board) -> dict:
    ports = list_ports()
    if not args.json:
        if not ports:
            print("No serial ports found. Is the USB-TTL adapter plugged in?")
        for p in ports:
            print(f"  {p['device']:<16} {p['description']}  [{p['hwid']}]")
        if not IS_WIN and Path("/dev/serial/by-id").exists():
            print("\nPersistent names (recommended for bench.toml):")
            for p in sorted(Path("/dev/serial/by-id").iterdir()):
                print(f"  {p}  ->  {os.path.realpath(p)}")
    return ok_result("ports", f"Found {len(ports)} serial ports.", ports=ports)


def cmd_doctor(cfg, args, board) -> dict:
    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"check": name, "ok": ok, "detail": detail, "required": required})

    add("python", True, sys.version.split()[0])
    try:
        import serial
        add("pyserial", True, serial.__version__)
    except ImportError:
        add("pyserial", False, "not installed: py -m pip install pyserial")
    add("config", True, str(cfg.path) if cfg.path else "(defaults, --sim)")

    backend = cfg.get("probe", "backend")
    try:
        prog = make_programmer(cfg, board)
        add("programmer", True, f"{backend}: {getattr(prog, 'exe', 'sim')}")
        try:
            add("probe", True, prog.probe_check())
        except BenchError as e:
            add("probe", False, e.summary + " | " + "; ".join(e.likely_causes[:2]))
    except BenchError as e:
        add("programmer", False, e.summary)

    port = cfg.get("serial", "port")
    if backend == "sim":
        add("serial", True, "sim")
    elif not port:
        add("serial", False, "[serial] port is empty in bench.toml")
    else:
        try:
            devs = {p["device"] for p in list_ports()}
            real = os.path.realpath(port) if not IS_WIN else port
            found = port in devs or real in devs or (not IS_WIN and Path(port).exists())
            add("serial", found, f"{port} {'found' if found else 'not in the list (bench.py ports)'}")
        except BenchError as e:
            add("serial", False, e.summary)

    golden = cfg.resolve(cfg.get("recovery", "golden_image"))
    add("golden_image", golden.is_file(), str(golden) if golden.is_file() else
        f"{golden} missing: python firmware/build.py", required=False)

    gcc = shutil.which("arm-none-eabi-gcc") or find_tool(None, [], [
        r"C:\ST\STM32CubeCLT*\GNU-tools-for-STM32\bin\arm-none-eabi-gcc.exe",
        r"C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
        r"C:\Program Files\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
        "/opt/st/stm32cubeclt*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc"])
    add("arm-none-eabi-gcc", bool(gcc), gcc or "not found (needed to build the firmware)", required=False)

    ok = all(c["ok"] for c in checks if c["required"])
    if not args.json:
        for c in checks:
            mark = "OK  " if c["ok"] else ("FAIL" if c["required"] else "WARN")
            print(f"  [{mark}] {c['check']:<18} {c['detail']}")
        print()
    return {"ok": ok, "command": "doctor", "checks": checks,
            "summary": "Bench ready." if ok else "Something is missing; see the FAIL lines above.",
            **({} if ok else {"error_type": "not_ready"})}


def cmd_monitor(cfg, args, board) -> dict:
    sess = open_session(cfg, board)
    print(f"Serial monitor ({cfg.get('serial', 'port')}). Press Ctrl+C to exit.")
    t_end = time.monotonic() + args.seconds if args.seconds else None
    try:
        while t_end is None or time.monotonic() < t_end:
            sess._pump(0.1)
            while sess.pending:
                print(f"{time.strftime('%H:%M:%S')}  {sess.pending.pop(0)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sess.close()
    return ok_result("monitor", "Monitor closed.")


def cmd_audit(cfg, args, board) -> dict:
    return audit_verify(cfg)


# ---------------------------------------------------------------------------
# Production station: program and final-test board by board
# ---------------------------------------------------------------------------

STATION_CSV_FIELDS = ["time", "board_no", "operator", "uid", "firmware_sha256", "result", "failed_step",
                      "report_dirs", "repeat", "audit_hash"]

BIG = {
    "P": ["█████ ", "█   █ ", "█████ ", "█     ", "█     "],
    "A": [" ███  ", "█   █ ", "█████ ", "█   █ ", "█   █ "],
    "S": [" ████ ", "█     ", " ███  ", "    █ ", "████  "],
    "F": ["█████ ", "█     ", "████  ", "█     ", "█     "],
    "I": ["█████ ", "  █   ", "  █   ", "  █   ", "█████ "],
    "L": ["█     ", "█     ", "█     ", "█     ", "█████ "],
}


def big_text(word: str, color: bool) -> str:
    rows = ["  " + "".join(BIG[c][i] for c in word) for i in range(5)]
    code = "\033[1;32m" if word == "PASS" else "\033[1;41;97m"
    return "\n".join(f"{code}{r}\033[0m" if color else r for r in rows)


def station_csv_kind(cfg: Config) -> str:
    """Keeps simulated-board trials out of the real production log."""
    return "_sim" if cfg.get("probe", "backend") == "sim" else ""


def station_previous(cfg: Config, uid: str) -> list[str]:
    """Times of earlier station records for this UID (from all station_*.csv files of the same kind).
    Files written before the columns were renamed use "zaman" instead of "time"."""
    found = []
    sim = station_csv_kind(cfg) == "_sim"
    for f in sorted(cfg.runs_dir.glob("station_*.csv")):
        if f.stem.endswith("_sim") != sim:
            continue
        try:
            with f.open(encoding="utf-8", newline="") as fh:
                found += [t for row in csv.DictReader(fh)
                          if row.get("uid") == uid and (t := row.get("time") or row.get("zaman"))]
        except (OSError, KeyError, csv.Error):
            continue
    return found


def station_csv_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as fh:
        return next(csv.reader(fh), [])


def station_csv_append(cfg: Config, row: dict) -> Path:
    base, kind = f"station_{dt.date.today():%Y%m%d}", station_csv_kind(cfg)
    path = cfg.runs_dir / f"{base}{kind}.csv"
    if path.exists() and station_csv_header(path) != STATION_CSV_FIELDS:
        # today's file has an older header (e.g. the Turkish column names): do not mix columns
        path = cfg.runs_dir / f"{base}_v2{kind}.csv"
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=STATION_CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return path


def station_card(cfg: Config, plans: list, board, operator: str, card_no: int) -> dict:
    """One board: runs the plans in order and stops at the first failed plan. Writes the audit entry."""
    results = []
    for plan_path, plan in plans:
        if board is not None:
            board.out.clear()
        r = execute_plan(cfg, plan_path, plan, board, operator=operator)
        results.append(r)
        if not r["ok"]:
            break
    ok = all(r["ok"] for r in results) and len(results) == len(plans)
    uid = next((r["meta"].get("uid") for r in results if r["meta"].get("uid")), None) or ""
    fw = next((fw["sha256"] for r in results for fw in r["meta"]["firmware"] if fw.get("sha256")), "") or ""
    failed = ""
    if not ok:
        bad = results[-1]
        st = next((x for x in bad["steps"] if not x["ok"]), None)
        failed = (f"{bad['plan']}: step {st['step']} {st['name']} ({st['error_type']})" if st
                  else f"{bad['plan']}: {bad.get('error_type')}")
    prev = station_previous(cfg, uid) if uid else []
    dirs = [r["report_dir"] for r in results]
    h = audit_append(cfg, {"cmd": "station-card", "card_no": card_no, "operator": operator, "uid": uid,
                           "firmware_sha256": fw, "ok": ok, "failed_step": failed or None,
                           "report_dirs": dirs, "repeat_of": prev or None})
    for r in results:
        stamp_audit_hash(r, h)
    row = {"time": dt.datetime.now().isoformat(timespec="seconds"), "board_no": card_no, "operator": operator,
           "uid": uid or "unreadable", "firmware_sha256": fw, "result": "PASS" if ok else "FAIL",
           "failed_step": failed, "report_dirs": ";".join(dirs),
           "repeat": f"REPEAT: {len(prev)} previous tests, first {prev[0]}" if prev else "", "audit_hash": h}
    csv_path = station_csv_append(cfg, row)
    return {**row, "ok": ok, "csv": str(csv_path), "repeat_count": len(prev),
            "plans": [{"plan": r["plan"], "ok": r["ok"], "summary": r["summary"], "report_dir": r["report_dir"]}
                      for r in results]}


def cmd_station(cfg, args, board) -> dict:
    out = sys.stderr if args.json else sys.stdout
    color = out.isatty() and not os.environ.get("NO_COLOR")

    def say(text: str = ""):
        print(text, file=out, flush=True)

    if args.no_prompt and not args.rounds:
        raise BenchError("invalid_args", "--no-prompt also needs --rounds N (otherwise the loop never ends).")
    names = [n.strip() for n in args.plans.split(",") if n.strip()]
    plans = [load_plan(cfg, n) for n in names]  # all are validated up front

    operator = (args.operator or "").strip()
    if not operator:
        if args.no_prompt:
            raise BenchError("invalid_args", "--no-prompt needs --operator NAME (for traceability).")
        while not operator:
            operator = input("Operator name: ").strip()
    say(f"Station: {cfg.get('bench', 'name')} | operator: {operator} | plans: {', '.join(names)}")

    cards = []
    interrupted = False
    try:
        while not args.rounds or len(cards) < args.rounds:
            n = len(cards) + 1
            if not args.no_prompt:
                try:
                    ans = input(f"\n[{n}] Connect the board, press Enter (q to quit): ").strip().lower()
                except EOFError:
                    break
                if ans == "q":
                    break
            else:
                say(f"\n[{n}] testing board...")
            c = station_card(cfg, plans, board, operator, n)
            cards.append(c)
            say(big_text("PASS" if c["ok"] else "FAIL", color))
            say(f"  UID {c['uid']}  firmware {c['firmware_sha256'][:16]}…")
            for pr in c["plans"]:
                say(f"  [{'PASS' if pr['ok'] else 'FAIL'}] {pr['summary']}")
            if c["failed_step"]:
                say(f"  Failed step: {c['failed_step']}")
            if c["repeat_count"]:
                say(f"  !!! THIS BOARD WAS TESTED BEFORE: {c['repeat']}")
    except KeyboardInterrupt:
        interrupted = True
        say("\nStopped.")

    n_pass = sum(c["ok"] for c in cards)
    n_rep = sum(1 for c in cards if c["repeat_count"])
    summary = (f"Station: {len(cards)} boards, {n_pass} PASS, {len(cards) - n_pass} FAIL"
               + (f", {n_rep} retested" if n_rep else "") + (" (stopped)" if interrupted else "") + ".")
    res = ok_result("station", summary, operator=operator, plans=names, cards=cards,
                    csv=cards[-1]["csv"] if cards else None)
    if len(cards) != n_pass:
        res.update(ok=False, error_type="cards_failed",
                   likely_causes=["See the report folders and serial.log files of the failed boards"])
    return res


COMMANDS = {
    "doctor": (cmd_doctor, False), "ports": (cmd_ports, False), "flash": (cmd_flash, True),
    "reset": (cmd_reset, True), "send": (cmd_send, True), "read": (cmd_read, True),
    "run": (cmd_run, True), "recover": (cmd_recover, True), "monitor": (cmd_monitor, True),
    "audit": (cmd_audit, False), "halt-dump": (cmd_halt_dump, True), "mem": (cmd_mem, True),
    "gdbserver": (cmd_gdbserver, True), "station": (cmd_station, True),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench.py", description="STM32 test bench tool (Windows + Linux)")
    p.add_argument("--config", help="Path to bench.toml (default: the one next to bench.py)")
    p.add_argument("--json", action="store_true", help="Print the result as JSON (for agents)")
    p.add_argument("--sim", action="store_true", help="Run with a simulated board, no hardware")
    p.add_argument("--version", action="version", version=f"tezgah {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="Check the setup and connections")
    sub.add_parser("ports", help="List serial ports")

    s = sub.add_parser("flash", help="Flash firmware (.elf/.hex/.bin)")
    s.add_argument("image")
    s.add_argument("--under-reset", action="store_true", help="Connect under reset (for a locked-up board)")

    s = sub.add_parser("reset", help="Reset the board")
    s.add_argument("--expect", help="Line expected after reset (regex), e.g. '^BOOT'")
    s.add_argument("--timeout", type=float, default=3)

    s = sub.add_parser("send", help="Send a command over the serial port")
    s.add_argument("text")
    s.add_argument("--expect", help="Expected reply (regex)")
    s.add_argument("--timeout", type=float, default=2)
    s.add_argument("--min", type=float, help="Lower bound for the captured number")
    s.add_argument("--max", type=float, help="Upper bound for the captured number")

    s = sub.add_parser("read", help="Read from the serial port")
    s.add_argument("--seconds", type=float, default=2)
    s.add_argument("--until", help="Stop when a line matches this pattern (regex)")

    s = sub.add_parser("run", help="Run a test plan (plans/<name>.toml)")
    s.add_argument("plan")
    s.add_argument("--verify-can-fail", action="store_true",
                   help="If it passes, run again with a deliberately broken expectation; that run must FAIL")

    sub.add_parser("recover", help="Recover the board by flashing the golden firmware under reset")

    s = sub.add_parser("station", help="Production station: program and final-test board by board (q to quit)")
    s.add_argument("--plans", default="smoke,measure", help="Comma-separated plans (default: smoke,measure)")
    s.add_argument("--operator", help="Operator name (asked at start if not given)")
    s.add_argument("--rounds", type=int, default=0, help="Stop after this many boards (0: until q)")
    s.add_argument("--no-prompt", action="store_true", help="Do not wait for Enter (for testing; needs --rounds and --operator)")

    s = sub.add_parser("monitor", help="Live serial monitor (for humans)")
    s.add_argument("--seconds", type=float, default=0)

    sub.add_parser("audit", help="Check the integrity of the audit log")

    s = sub.add_parser("halt-dump", help="Halt the core; read registers, fault registers and the stack, then resume")
    s.add_argument("--keep-halted", action="store_true", help="Leave the core halted after reading")
    s.add_argument("--elf", help="ELF for symbols (default: [recovery] golden_image)")

    s = sub.add_parser("mem", help="Read memory/registers (read-only, allowed regions)")
    s.add_argument("addr", help="Start address, e.g. 0x40020C14")
    s.add_argument("--count", type=int, default=1, help=f"Number of 32-bit words (at most {MEM_MAX_WORDS})")

    s = sub.add_parser("gdbserver", help="Start a GDB server (for humans only, stop with Ctrl+C)")
    s.add_argument("--port", type=int, default=3333)
    s.add_argument("--elf", help="ELF to suggest to gdb (default: [recovery] golden_image)")
    return p


def stamp_audit_hash(result: dict, h: str) -> None:
    """The audit entry is written after the report; add its hash to the report and result.json afterwards."""
    dirs = [result.get("report_dir"), (result.get("negative_check") or {}).get("report_dir")]
    for d in filter(None, dirs):
        rep = Path(d) / "report.md"
        if rep.exists():
            rep.write_text(rep.read_text(encoding="utf-8").replace(
                AUDIT_PENDING, f"- Audit log hash: `{h}` (runs/audit.jsonl)"), encoding="utf-8")
        rj = Path(d) / "result.json"
        if rj.exists():
            data = json.loads(rj.read_text(encoding="utf-8"))
            data["audit_hash"] = h
            rj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    fn, needs_lock = COMMANDS[args.cmd]
    cfg = None
    try:
        cfg = load_config(args.config, args.sim)
        board = SimBoard(cfg) if cfg.get("probe", "backend") == "sim" else None
        if needs_lock:
            with BenchLock(cfg):
                result = fn(cfg, args, board)
        else:
            result = fn(cfg, args, board)
    except BenchError as e:
        result = err_result(args.cmd, e)
    except KeyboardInterrupt:
        result = {"ok": False, "command": args.cmd, "error_type": "interrupted", "summary": "Stopped by the user."}

    if cfg is not None and args.cmd not in ("ports", "doctor", "audit", "monitor"):
        try:
            h = audit_append(cfg, {"cmd": args.cmd, "args": {k: v for k, v in vars(args).items()
                                                            if k not in ("cmd",) and v not in (None, False)},
                                   "ok": result.get("ok"), "summary": result.get("summary"),
                                   "report_dir": result.get("report_dir")})
            result["audit_hash"] = h
            stamp_audit_hash(result, h)
        except Exception:
            pass
    if args.cmd == "run" and not args.json:
        for s in result.get("steps", []):
            print(f"  [{'PASS' if s['ok'] else 'FAIL'}] {s['step']:>2}. {s['name']:<22} {s['summary']}")
        rec = result.get("recovery", {})
        if rec.get("attempted"):
            print(f"  Recovery: {'succeeded' if rec.get('ok') else 'FAILED, a human must step in'}")
        neg = result.get("negative_check")
        if neg:
            print(f"  Negative check: {'FAILED (good)' if neg.get('failed_as_expected') else 'PASSED (bad)'}")
    emit(result, args.json)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
