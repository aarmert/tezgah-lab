#!/usr/bin/env python3
"""
stm32bench - STM32 test tezgahi araci (Windows + Linux)

Bir kartı yükler, resetler, seri porttan konuşur ve test planlarını
çalıştırır. Hem insan hem de bir yapay zeka ajanı (--json ile) kullanabilir.

Hızlı başlangıç:
    python bench.py doctor          # her şey hazır mı?
    python bench.py ports           # seri portları listele
    python bench.py run smoke       # plans/smoke.toml planını çalıştır
    python bench.py --sim run smoke # donanım olmadan sahte kartla dene

Gereken: Python 3.11+, pyserial, STM32CubeProgrammer CLI (veya OpenOCD).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("stm32bench: Python 3.11 veya daha yeni gerekli (tomllib için).")

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

VERSION = "0.3.0"
IS_WIN = os.name == "nt"
SCRIPT_DIR = Path(__file__).resolve().parent

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Hata ve sonuç yapısı
# ---------------------------------------------------------------------------

class BenchError(Exception):
    """Ajanın anlayabileceği, yapılandırılmış hata."""

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
    mark = "OK  " if result.get("ok") else "HATA"
    print(f"[{mark}] {result.get('summary', '')}")
    if not result.get("ok"):
        if result.get("error_type"):
            print(f"       tür: {result['error_type']}")
        for c in result.get("likely_causes", []):
            print(f"       olası sebep: {c}")
        if result.get("details"):
            print("       ayrıntı:")
            for line in str(result["details"]).splitlines()[-15:]:
                print(f"         {line}")
    for key in ("lines", "response"):
        if result.get(key):
            print("       seri port:")
            for line in result[key]:
                print(f"         {line}")
    if result.get("report_dir"):
        print(f"       rapor: {result['report_dir']}")


# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "bench": {"name": "bench"},
    "probe": {"backend": "cubeprog", "connect_mode": "NORMAL", "timeout_s": 90},
    "serial": {"port": "", "baud": 115200},
    "recovery": {"golden_image": "firmware/build/stm32bench-fw.elf",
                 "banner": "^BOOT ", "banner_timeout_s": 3},
    "permissions": {"allow_flash": True, "allow_reset": True, "plans_dir_only": False},
    "paths": {"plans_dir": "plans", "runs_dir": "runs"},
    # cam_blink: android-camera-tool ile video. roi = [x, y, genişlik, yükseklik] (piksel) verilirse
    # LED bölgesi otomatik aranmaz.
    "camera": {"tool_dir": "~/android-camera-tool", "bun": "~/.bun/bin/bun", "serial": "",
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
                raise BenchError("config_invalid", f"{c} okunamadı: {e}",
                                 ["TOML yazım hatası (tırnak, köşeli parantez)"])
            cfg = Config(c.resolve(), c.resolve().parent, data)
            break
    else:
        if not sim:
            raise BenchError(
                "config_not_found", "bench.toml bulunamadı.",
                ["bench.example.toml dosyasını bench.toml olarak kopyalayıp seri portu düzenle",
                 "Başka bir yerdeyse --config veya BENCH_CONFIG ile göster"])
        cfg = Config(None, SCRIPT_DIR, {})

    if sim:
        cfg.data.setdefault("probe", {})["backend"] = "sim"
        cfg.data.setdefault("serial", {})["port"] = "sim"
    return cfg


# ---------------------------------------------------------------------------
# Denetim kaydı (SHA-256 zincirli) ve kilit
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
        return ok_result("audit", "Henüz kayıt yok.", entries=0)
    prev = "0" * 64
    n = 0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        h = rec.pop("hash", None)
        if rec.get("prev") != prev:
            raise BenchError("audit_broken", f"Denetim zinciri {i}. satırda kopuk (prev uyuşmuyor).",
                             ["Kayıt dosyası elle değiştirilmiş veya satır silinmiş"])
        payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        if hashlib.sha256((prev + payload).encode("utf-8")).hexdigest() != h:
            raise BenchError("audit_broken", f"Denetim zinciri {i}. satırda bozuk (hash uyuşmuyor).",
                             ["Bu satırın içeriği sonradan değiştirilmiş"])
        prev = h
        n += 1
    return ok_result("audit", f"Denetim zinciri sağlam ({n} kayıt).", entries=n)


class BenchLock:
    """Aynı anda iki işlemin karta erişmesini engeller (Windows + Linux).

    İşletim sisteminin dosya kilidini kullanır (Linux: flock, Windows: msvcrt.locking).
    Kilidi tutan işlem ne kadar uzun sürerse sürsün kilit onda kalır; işlem ölürse
    (çökme, kill, Ctrl+C) kilidi işletim sistemi kendisi bırakır. Bu yüzden "eski kilit"
    tahmini ve dosya silme gerekmez. Kilit dosyası hiç silinmez, içinde sadece bilgi
    amaçlı tutan işlemin PID'i yazar.
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
                raise BenchError("bench_busy", f"Tezgah başka bir işlem tarafından kullanılıyor (pid {holder}).",
                                 ["Başka bir bench.py komutu çalışıyor (monitor, read, gdbserver açık kalmış olabilir)",
                                  "O işlem bitince kilit kendiliğinden açılır; dosyayı silmek gerekmez"])
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
        self.f.close()  # Linux'ta flock, dosya kapanınca kalkar
        self.f = None


# ---------------------------------------------------------------------------
# Harici araç bulma ve çalıştırma
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
        raise BenchError("tool_not_found", f"Program bulunamadı: {cmd[0]}",
                         ["Kurulu değil veya PATH'te değil", "bench.toml içinde tam yolu ver"])
    except subprocess.TimeoutExpired:
        raise BenchError("timeout", f"{Path(cmd[0]).name} {timeout:.0f} saniyede bitmedi ve durduruldu.",
                         ["Probe takıldı: USB kablosunu çıkarıp tak",
                          "Kart beslemesi yok veya kararsız"])
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# ---------------------------------------------------------------------------
# Programlayıcılar: STM32CubeProgrammer, OpenOCD, Sim
# ---------------------------------------------------------------------------

CAUSES_PROBE = ["USB kablosu takılı değil ya da sadece şarj kablosu",
                "Windows: ST-Link sürücüsü kurulu değil (STM32CubeProgrammer kurulumu getirir)",
                "Linux: ST-Link udev kuralı yok veya kullanıcı plugdev grubunda değil",
                "Probe başka bir programda açık (CubeIDE, CubeProgrammer arayüzü, OpenOCD)"]
CAUSES_TARGET = ["Kartın beslemesi yok",
                 "Firmware SWD pinlerini (PA13/PA14) başka amaçla kullanıyor: 'bench.py recover' çalıştır",
                 "Çip uyku modunda veya kilitlenmiş: 'bench.py recover' çalıştır",
                 "Discovery kartında CN3 jumperları takılı değil"]


class Programmer:
    name = "base"

    def flash(self, image: Path, under_reset: bool = False) -> str: ...
    def reset(self) -> str: ...
    def probe_check(self) -> str: ...

    def debug_commands(self, cmds: list[str], timeout: float = 30) -> str:
        """Hata ayıklama komutları (halt, reg, mdw). Şimdilik sadece OpenOCD destekler."""
        raise BenchError("not_supported", f"Bu komut '{self.name}' backend'inde desteklenmiyor.",
                         ["bench.toml [probe] backend = \"openocd\" yap (Windows'ta da OpenOCD kurulabilir)"])

    def tool_version(self) -> str:
        """Programlayıcı aracının sürümü (rapor başlığı için)."""
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
        """Çipin 96 bitlik UID'sini debug portundan okur (çekirdek durdurulmaz)."""
        words = parse_mdw(self.debug_commands([f"mdw 0x{UID_ADDR:08X} 3"]))
        if len(words) != 3:
            raise BenchError("tool_error", f"UID için 3 kelime istendi, {len(words)} okundu.")
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
            raise BenchError("tool_not_found", "STM32_Programmer_CLI bulunamadı.",
                             ["STM32CubeProgrammer veya STM32CubeCLT kurulu değil",
                              "Kuruluysa bench.toml [probe] cubeprog_path ile tam yolu ver"])

    def _conn(self, mode: str) -> list[str]:
        c = ["-c", "port=SWD", f"mode={mode}"]
        if self.sn:
            c.append(f"sn={self.sn}")
        return c

    @staticmethod
    def _classify(rc: int, out: str, verify: bool) -> None:
        low = out.lower()
        if re.search(r"no (st-?link|debug probe) detected|no st-?link found|unable to open probe|dev_usb_comm_err", low):
            raise BenchError("probe_not_found", "ST-Link probe bulunamadı.", CAUSES_PROBE, tail(out))
        if re.search(r"no stm32 target found|unable to get core id|target not found|dev_connect_err|failed to connect", low):
            raise BenchError("target_not_responding", "ST-Link bulundu ama hedef çip cevap vermiyor.",
                             CAUSES_TARGET, tail(out))
        if re.search(r"file .*(not found|does not exist)|cannot open file|wrong file", low):
            raise BenchError("image_not_found", "Yüklenecek dosya bulunamadı veya okunamadı.",
                             ["Önce firmware'i derle: python firmware/build.py"], tail(out))
        if re.search(r"read.?out protection|rdp", low) and rc != 0:
            raise BenchError("read_protected", "Çipte okuma koruması (RDP) aktif.",
                             ["Bu araç option byte'lara bilerek dokunmaz; bir insan CubeProgrammer ile kontrol etmeli"],
                             tail(out))
        if re.search(r"verif\w* (failed|error)|data mismatch|mismatch found", low):
            raise BenchError("verify_failed", "Yazma sonrası doğrulama başarısız.",
                             ["Flash sektörü yazma korumalı", "Besleme kararsız", "Kablo/bağlantı gevşek"],
                             tail(out))
        err_lines = [l.strip() for l in out.splitlines() if re.match(r"\s*error", l, re.I)]
        if rc != 0 or err_lines:
            raise BenchError("tool_error", err_lines[0] if err_lines else f"STM32_Programmer_CLI hata kodu {rc}",
                             ["Ayrıntıya bak; gerekirse 'bench.py recover' dene"], tail(out))

    def flash(self, image: Path, under_reset: bool = False) -> str:
        mode = "UR" if under_reset else self.mode
        rc, out = run_tool([self.exe, *self._conn(mode), "-w", str(image), "-v", "-rst"], self.timeout)
        self._classify(rc, out, verify=True)
        return f"{image.name} yüklendi ve doğrulandı (mod={mode})."

    def reset(self) -> str:
        rc, out = run_tool([self.exe, *self._conn(self.mode), "-rst"], 30)
        self._classify(rc, out, verify=False)
        return "Kart resetlendi."

    def probe_check(self) -> str:
        rc, out = run_tool([self.exe, "-l", "st-link"], 30)
        sns = re.findall(r"ST-?LINK SN\s*:\s*(\S+)", out, re.I)
        if not sns:
            raise BenchError("probe_not_found", "Bağlı ST-Link yok.", CAUSES_PROBE, tail(out))
        return f"ST-Link bulundu: {', '.join(sns)}"


class OpenOCD(Programmer):
    name = "openocd"

    def __init__(self, cfg: Config):
        self.exe = find_tool(cfg.get("probe", "openocd_path"), ["openocd", "openocd.exe"], OPENOCD_GLOBS)
        self.interface = cfg.get("probe", "openocd_interface", "interface/stlink.cfg")
        self.target = cfg.get("probe", "openocd_target", "target/stm32f4x.cfg")
        self.sn = cfg.get("probe", "serial_number")
        self.timeout = float(cfg.get("probe", "timeout_s"))
        if not self.exe:
            raise BenchError("tool_not_found", "openocd bulunamadı.",
                             ["Linux: sudo apt install openocd", "bench.toml [probe] openocd_path ile yolu ver"])

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
            raise BenchError("probe_not_found", "ST-Link probe bulunamadı.", CAUSES_PROBE, tail(out))
        if re.search(r"init mode failed|unable to connect to the target|error connecting dp|target not examined", low):
            raise BenchError("target_not_responding", "Probe bulundu ama hedef çip cevap vermiyor.",
                             CAUSES_TARGET, tail(out))
        if re.search(r"couldn't open|no such file", low):
            raise BenchError("image_not_found", "Yüklenecek dosya açılamadı.", [], tail(out))
        if re.search(r"verify failed|checksum mismatch", low):
            raise BenchError("verify_failed", "Doğrulama başarısız.", [], tail(out))
        if rc != 0:
            raise BenchError("tool_error", f"openocd hata kodu {rc}", [], tail(out))

    def flash(self, image: Path, under_reset: bool = False) -> str:
        cmd = self._base(under_reset) + ["-c", f"program {{{image.resolve().as_posix()}}} verify reset exit"]
        rc, out = run_tool(cmd, self.timeout)
        self._classify(rc, out)
        return f"{image.name} yüklendi ve doğrulandı."

    def reset(self) -> str:
        rc, out = run_tool(self._base(False) + ["-c", "init; reset run; shutdown"], 30)
        self._classify(rc, out)
        return "Kart resetlendi."

    def probe_check(self) -> str:
        rc, out = run_tool(self._base(False) + ["-c", "init; shutdown"], 30)
        self._classify(rc, out)
        return "OpenOCD probe ve hedefe bağlandı."

    def debug_commands(self, cmds: list[str], timeout: float = 30) -> str:
        cmd = self._base(False) + ["-c", "init"]
        for c in cmds:
            cmd += ["-c", c]
        rc, out = run_tool(cmd + ["-c", "shutdown"], timeout)
        self._classify(rc, out)
        return out

    def gdb_server_cmd(self, port: int) -> list[str]:
        # Sadece localhost'tan bağlanılır (OpenOCD 0.12 varsayılanı); telnet/tcl kapalı
        return self._base(False) + ["-c", f"gdb_port {port}", "-c", "telnet_port disabled",
                                    "-c", "tcl_port disabled"]


# --- Sahte kart: donanım olmadan aracı ve planları denemek için -------------

SIM_UID = "0033002A3438511234363632"
SIM_FW_VERSION = "1.2.0"


class SimBoard:
    """test firmware'inin davranışını taklit eder; durum runs/sim_state.json'da tutulur."""

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
        self.green = st.get("green", 2.0)   # yeşil LED: yanıp sönme Hz'i, "on" veya "off"
        self.out: list[str] = []

    def _save(self):
        self.state_path.write_text(json.dumps(
            {"image": self.image, "image_path": self.image_path, "crashed": self.crashed,
             "boot_t": self.boot_t, "green": self.green}), encoding="utf-8")

    def flash(self, image: Path):
        if not image.is_file():
            raise BenchError("image_not_found", f"{image} bulunamadı.",
                             ["Önce firmware'i derle: python firmware/build.py"])
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
        """Yeşil LED'i taklit eden sentetik video (30 fps): gri zemin, LED yerinde yanıp sönen kare."""
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
            raise BenchError("camera_failed", "[sim] sentetik video üretilemedi (ffmpeg).", [], tail(p.stderr))

    def _fault_pc(self) -> int:
        """Gerçek firmware'deki 'udf' komutunun adresini bul (yoksa 0)."""
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
        return f"[sim] {image.name} yüklendi ve doğrulandı."

    def reset(self) -> str:
        self.board.reset()
        return "[sim] Kart resetlendi."

    def probe_check(self) -> str:
        return "[sim] Sahte probe hazır."

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
    raise BenchError("config_invalid", f"Bilinmeyen backend: {backend}", ["cubeprog, openocd veya sim olmalı"])


# ---------------------------------------------------------------------------
# Seri port: gerçek ve sahte
# ---------------------------------------------------------------------------

CAUSES_SERIAL = ["Port adı yanlış: 'bench.py ports' ile listele",
                 "Port başka bir programda açık (PuTTY, Arduino IDE, CubeIDE seri monitörü)",
                 "USB-TTL çıkarılmış",
                 "Linux: kullanıcı dialout grubunda değil"]


class SerialTransport:
    def __init__(self, port: str, baud: int):
        try:
            import serial  # noqa: F401
        except ImportError:
            raise BenchError("pyserial_missing", "pyserial kurulu değil.",
                             ["Windows: py -m pip install pyserial", "Linux: sudo apt install python3-serial"])
        if not port:
            raise BenchError("config_invalid", "bench.toml içinde [serial] port boş.",
                             ["'bench.py ports' ile portu bul ve yaz (Windows: COM5, Linux: /dev/ttyUSB0)"])
        self.port, self.baud = port, baud
        self.ser = None
        self._open()

    def _open(self):
        import serial
        try:
            # serial_for_url: COM5, /dev/ttyUSB0 ve ağ üzerinden socket://host:port adreslerini destekler
            self.ser = serial.serial_for_url(self.port, baudrate=self.baud, timeout=0.05, write_timeout=1)
        except serial.SerialException as e:
            raise BenchError("serial_open_failed", f"{self.port} açılamadı.", CAUSES_SERIAL, str(e))

    def _reopen(self):
        """Port bir anlığına kaybolursa (USB yeniden tanındı) tekrar bağlan."""
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
                    break  # read(1) port zaman aşımı (50 ms) kadar bloklardı
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
    """Satır bazlı seri haberleşme + zaman damgalı döküm."""

    def __init__(self, transport):
        self.t = transport
        self.buf = ""
        self.pending: list[str] = []
        self.transcript: list[tuple[float, str, str]] = []
        self.t0 = time.monotonic()
        self.last_fault: str | None = None
        self.run_dir: Path | None = None   # cam_blink dosyaları buraya
        self.step_no = 0
        self.cam_last: dict | None = None  # son yanıp sönme ölçümünün bölgesi ve seviyeleri (steady için)

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
        return "\n".join(f"{d} {txt}" for _, d, txt in self.transcript[-n:]) or "(hiç yok)"

    def note(self, text: str):
        self.transcript.append((time.monotonic() - self.t0, "#", text))

    def drain(self):
        """Bekleyen eski satırları at (yeni komutun cevabıyla karışmasın). Beklemez: sadece gelmiş olanı okur."""
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
# Test planı çalıştırıcı
# ---------------------------------------------------------------------------

CAM_SMALL_W = 320       # LED arama ve ölçüm bu genişliğe küçültülmüş karelerde yapılır
CAM_ROI_SMALL = 5       # LED bölgesinin kenarı (küçük karede piksel)
CAM_MIN_CONTRAST = 30   # yanıp sönme sayılmak için en az parlaklık farkı (0-255)
ACTIONS = {"flash", "reset", "expect", "send", "silence", "delay", "clock_check", "cam_blink"}
TICK_PATTERN = r"^TICK (\d+)$"
NEVER_MATCH = "__BU_SATIR_ESLESMEMELI__"


def load_plan(cfg: Config, name_or_path: str) -> tuple[Path, dict]:
    p = Path(name_or_path)
    if not p.suffix:
        p = cfg.plans_dir / f"{name_or_path}.toml"
    elif not p.is_absolute():
        p = (Path.cwd() / p) if (Path.cwd() / p).exists() else cfg.resolve(p)
    p = p.resolve()
    if cfg.get("permissions", "plans_dir_only") and cfg.plans_dir.resolve() not in p.parents:
        raise BenchError("permission_denied", f"Sadece {cfg.plans_dir} içindeki planlar çalıştırılabilir.",
                         ["Yeni planı bir insanın onaylayıp plans/ klasörüne koyması gerekir"])
    if not p.is_file():
        raise BenchError("plan_not_found", f"Plan bulunamadı: {p}", [f"{cfg.plans_dir} içindeki planlara bak"])
    try:
        plan = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise BenchError("plan_invalid", f"{p.name} okunamadı: {e}")
    steps = plan.get("step", [])
    if not steps:
        raise BenchError("plan_invalid", f"{p.name} içinde hiç [[step]] yok.")
    for i, s in enumerate(steps, 1):
        if s.get("action") not in ACTIONS:
            raise BenchError("plan_invalid", f"{p.name} adım {i}: bilinmeyen action '{s.get('action')}'",
                             [f"Geçerli olanlar: {', '.join(sorted(ACTIONS))}"])
        if s["action"] == "clock_check":
            rng = s.get("range")
            if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(v, (int, float)) for v in rng)):
                raise BenchError("plan_invalid", f"{p.name} adım {i}: clock_check için range = [alt, üst] gerekli.")
            if not isinstance(s.get("seconds"), (int, float)) or s["seconds"] <= 0:
                raise BenchError("plan_invalid", f"{p.name} adım {i}: clock_check için seconds > 0 gerekli.")
        if s["action"] == "cam_blink":
            steady = s.get("steady")
            if steady not in (None, "on", "off"):
                raise BenchError("plan_invalid", f"{p.name} adım {i}: cam_blink steady 'on' veya 'off' olmalı.")
            for key in ("range", "duty_range"):
                rng = s.get(key)
                if rng is not None and not (isinstance(rng, list) and len(rng) == 2
                                            and all(isinstance(v, (int, float)) for v in rng)):
                    raise BenchError("plan_invalid", f"{p.name} adım {i}: cam_blink {key} = [alt, üst] olmalı.")
            if steady is None and s.get("range") is None:
                raise BenchError("plan_invalid", f"{p.name} adım {i}: cam_blink için range (Hz) veya steady gerekli.")
        for key in ("pattern", "expect"):
            if key in s:
                try:
                    re.compile(s[key])
                except re.error as e:
                    raise BenchError("plan_invalid", f"{p.name} adım {i}: geçersiz regex: {e}")
    return p, plan


def check_range(m: re.Match, rng) -> tuple[bool, str]:
    if not rng:
        return True, ""
    if not m.groups():
        return False, "range verilmiş ama desende yakalama grubu () yok"
    try:
        val = float(m.group(1))
    except (TypeError, ValueError):
        return False, f"yakalanan değer sayı değil: {m.group(1)!r}"
    lo, hi = rng
    if lo <= val <= hi:
        return True, f"değer {val:g} [{lo}, {hi}] aralığında"
    return False, f"değer {val:g} beklenen aralık [{lo}, {hi}] dışında"


def addr2line(elf: Path, addrs: list[int]) -> dict[int, str]:
    """Adresleri 'fonksiyon() dosya:satır' metnine çevirir (addr2line varsa). Çözülemeyenler sözlükte olmaz."""
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
    """HARDFAULT satırındaki pc adresini fonksiyon:dosya:satır'a çevirir (addr2line varsa)."""
    m = re.search(r"pc=0x([0-9A-Fa-f]{8})", fault_line)
    if not m:
        return None
    pc = int(m.group(1), 16)
    return addr2line(elf, [pc]).get(pc)


def check_permission(cfg: Config, key: str, what: str):
    if not cfg.get("permissions", key):
        raise BenchError("permission_denied", f"{what} izni kapalı (bench.toml [permissions] {key}).",
                         ["Bu izni sadece bir insan açabilir"])


def run_step(cfg: Config, prog: Programmer, sess: Session, plan: dict, step: dict) -> str:
    a = step["action"]
    timeout = float(step.get("timeout", 2))
    if a == "flash":
        check_permission(cfg, "allow_flash", "Yükleme")
        image = step.get("image") or plan.get("firmware")
        if not image:
            raise BenchError("plan_invalid", "flash adımı için 'image' veya planda 'firmware' gerekli.")
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
        return f"{step.get('seconds', 0.5)} sn beklendi."
    if a == "silence":
        secs = float(step.get("seconds", 1))
        lines = sess.collect(secs)
        if lines:
            raise BenchError("unexpected_output", f"{secs:g} sn sessizlik bekleniyordu ama {len(lines)} satır geldi.",
                             ["Kart hâlâ çalışıyor, beklenen çökme/uyku olmadı"], "\n".join(lines[:10]))
        return f"{secs:g} sn boyunca çıktı yok (beklenen)."

    if a == "clock_check":
        return clock_check(cfg, sess, plan, step, timeout)
    if a == "cam_blink":
        return cam_blink(cfg, prog, sess, step)

    pattern = step.get("pattern") if a == "expect" else step.get("expect")
    if a == "send":
        sess.drain()
        sess.send_line(step["text"])
        if not pattern:
            return f"'{step['text']}' gönderildi."
    m, line = expect_line(cfg, sess, plan, step, pattern, timeout,
                          f"'{step['text']}' komutuna " if a == "send" else "")
    good, msg = check_range(m, step.get("range"))
    if not good:
        raise BenchError("value_out_of_range", f"'{line}': {msg}", ["Ölçülen değeri ve beklenen aralığı kontrol et"])
    return f"'{line}'" + (f" ({msg})" if msg else "")


def expect_line(cfg: Config, sess: Session, plan: dict, step: dict, pattern: str, timeout: float,
                where: str = "") -> tuple[re.Match, str]:
    """Desene uyan satırı bekler; gelmezse çökme/zaman aşımı hatası verir."""
    m, line, seen = sess.wait_for(pattern, timeout)
    if m:
        return m, line
    if sess.last_fault:
        image = step.get("image") or plan.get("firmware")
        loc = resolve_fault(cfg.resolve(image), sess.last_fault) if image else None
        raise BenchError("target_crashed",
                         f"{where}cevap gelmedi; kart çökmüş: {sess.last_fault}",
                         ["Firmware HardFault'a düştü; pc adresindeki koda bak"] +
                         ([f"Çökme yeri: {loc}"] if loc else
                          ["pc'yi koda çevir: arm-none-eabi-addr2line -f -e firmware.elf <pc>"]),
                         "Son seri trafik:\n" + sess.recent())
    causes = ["Firmware bu komutu tanımıyor veya cevap formatı farklı",
              "Kart donmuş (sonsuz döngü) veya çökmüş",
              "Baud hızı yanlış (bozuk karakterler)",
              "TX/RX kabloları ters veya GND bağlı değil"] if not seen else \
             ["Cevap geldi ama beklenen desene uymadı: aşağıdaki satırlara bak"]
    raise BenchError("expect_timeout",
                     f"{where}{timeout:g} sn içinde /{pattern}/ desenine uyan satır gelmedi.",
                     causes, "Son seri trafik:\n" + sess.recent())


def read_tick(cfg: Config, sess: Session, plan: dict, step: dict, timeout: float) -> tuple[int, float, float]:
    """TICK gönderir. (kart ms, PC zamanı, gidiş-dönüş sn) döndürür.
    PC zamanı gönderme ile cevabın gelişinin ortası: kart sayacı bu aralıkta bir yerde okundu."""
    sess.drain()
    t_send = time.monotonic()
    sess.send_line("TICK")
    m, _ = expect_line(cfg, sess, plan, step, TICK_PATTERN, timeout, "'TICK' komutuna ")
    t_recv = time.monotonic()
    return int(m.group(1)), (t_send + t_recv) / 2, t_recv - t_send


def clock_check(cfg: Config, sess: Session, plan: dict, step: dict, timeout: float) -> str:
    """Kartın ms sayacını PC'nin monotonik saatiyle karşılaştırır: oran = kart süresi / PC süresi."""
    secs = float(step["seconds"])
    lo, hi = step["range"]
    tick0, pc0, rtt0 = read_tick(cfg, sess, plan, step, timeout)
    sess.note(f"clock_check: {secs:g} sn bekleniyor")
    time.sleep(max(0.0, pc0 + secs - time.monotonic()))
    tick1, pc1, rtt1 = read_tick(cfg, sess, plan, step, timeout)
    card_ms = tick1 - tick0
    pc_ms = (pc1 - pc0) * 1000
    # PC zamanının belirsizliği: her ölçümde gidiş-dönüşün yarısı
    unc_ms = (rtt0 + rtt1) / 2 * 1000
    times = f"kart {card_ms} ms, PC {pc_ms:.1f} ms (±{unc_ms:.1f} ms)"
    if card_ms <= 0:
        raise BenchError("target_reset", f"TICK ilerlemedi ({tick0} -> {tick1}); {times}.",
                         ["Kart ölçüm sırasında resetlendi (seri günlükte BOOT satırına bak)",
                          "SysTick kesmesi çalışmıyor"])
    ratio = card_ms / pc_ms
    msg = f"oran {ratio:.4f}: {times}"
    if not lo <= ratio <= hi:
        raise BenchError("value_out_of_range", f"saat oranı {ratio:.4f} beklenen aralık [{lo}, {hi}] dışında; {times}.",
                         ["SysTick reload değeri yanlış hesaplanmış (HSI_HZ / 1000 - 1 olmalı)",
                          "Saat kaynağı veya frekansı firmware'in varsaydığı gibi değil (HSI 16 MHz, PLL yok)",
                          "HSI kalibrasyonu bozuk (RCC_CR HSITRIM)",
                          "Kesmeler uzun süre kapalı kalıyor, SysTick kesmeleri kaçıyor"])
    return f"{msg} ([{lo}, {hi}] aralığında)"


# ---------------------------------------------------------------------------
# Kamera ile LED yanıp sönme ölçümü (cam_blink)
# ---------------------------------------------------------------------------

def record_video(cfg: Config, prog: Programmer, seconds: float, dest: Path) -> None:
    """Videoyu çekip dest'e taşır. Gerçek kartta android-camera-tool, sim'de ffmpeg ile sentetik video."""
    if isinstance(prog, SimProgrammer):
        prog.board.write_video(dest, seconds)
        return
    tool_dir = cfg.resolve(cfg.get("camera", "tool_dir"))
    bun = str(cfg.resolve(cfg.get("camera", "bun")))
    if not Path(bun).is_file():
        bun = shutil.which("bun") or bun
    cmd = [bun, "scripts/record-video.ts", cfg.get("camera", "serial") or "",
           cfg.get("camera", "camera"), f"{seconds:g}", str(cfg.get("camera", "fps"))]
    # serial boşsa kamera aracı tek bağlı cihazı seçer; birden fazla cihaz varsa hata verir
    try:
        p = subprocess.run(cmd, cwd=tool_dir, capture_output=True, text=True, errors="replace",
                           timeout=seconds + 90)
    except FileNotFoundError:
        raise BenchError("tool_not_found", f"Kamera aracı çalıştırılamadı: {bun} / {tool_dir}",
                         ["bench.toml [camera] tool_dir ve bun yollarını kontrol et"])
    except subprocess.TimeoutExpired:
        raise BenchError("camera_failed", "Kamera kaydı zaman aşımına uğradı.",
                         ["Telefon kilitli veya scrcpy takıldı", "adb devices ile telefonu kontrol et"])
    path = Path(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
    if p.returncode != 0 or not path or path.suffix != ".mp4" or not path.is_file():
        raise BenchError("camera_failed", "Kamera videosu çekilemedi.",
                         ["Telefon bağlı değil veya USB hata ayıklama izni yok (adb devices)",
                          "Birden fazla adb cihazı var: bench.toml [camera] serial ver",
                          "Telefon ekranı kilitli", f"Kamera {cfg.get('camera', 'fps')} fps desteklemiyor"],
                         tail(p.stderr + p.stdout))
    shutil.move(str(path), dest)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def video_frames(video: Path):
    """(kareler, zaman damgaları sn). Zamanlar ffprobe ile videonun kendi kare zaman damgalarından."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise BenchError("tool_not_found", "cam_blink için OpenCV ve NumPy gerekli.",
                         ["sudo apt install python3-opencv python3-numpy"])
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "frame=best_effort_timestamp_time,pts_time", "-of", "json", str(video)],
                       capture_output=True, text=True, timeout=60)
    try:
        ts = [float(f.get("best_effort_timestamp_time") or f["pts_time"])
              for f in json.loads(p.stdout)["frames"]]
    except (ValueError, KeyError, TypeError):
        raise BenchError("camera_failed", "Videonun kare zaman damgaları okunamadı (ffprobe).", [], tail(p.stderr))
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
                         f"Video kareleri ve zaman damgaları uyuşmuyor ({len(frames)} kare, {len(ts)} zaman).",
                         ["Video bozuk veya çok kısa"])
    return frames, np.array(ts) - ts[0]


def find_led_roi(small, scale: float) -> tuple[list[int], list[dict]]:
    """Kareler boyunca parlaklığı en çok değişen bölgeyi bulur. Tam çözünürlükte [x, y, w, h] ve
    ilk 3 aday döndürür (yansımalar gibi başka yanıp sönen yerleri görebilmek için)."""
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
    """Histerezisli eşik geçişleri; geçiş anı iki kare arasında doğrusal ara değerle bulunur."""
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
    sess.note(f"cam_blink: {secs:g} sn video -> {video.name}")
    record_video(cfg, prog, secs, video)
    frames, t = video_frames(video)

    h, w = frames[0].shape[:2]
    scale = CAM_SMALL_W / w
    # Renkten bağımsız parlaklık: kanalların en büyüğü (yeşil, kırmızı, mavi LED aynı şekilde görünür)
    small = np.stack([cv2.resize(f.max(axis=2), (CAM_SMALL_W, round(h * scale)), interpolation=cv2.INTER_AREA)
                      for f in frames]).astype(np.float32)
    use = t >= skip  # kameranın pozlama/odak oturması için baştaki kareler atlanır
    fixed_roi = cfg.get("camera", "roi")
    cands = []
    if fixed_roi:
        roi, roi_src = [int(v) for v in fixed_roi], "bench.toml"
    elif steady:
        if not sess.cam_last:
            raise BenchError("plan_invalid", "steady kontrolü için LED bölgesi bilinmiyor.",
                             ["Planda önce yanıp sönen bir cam_blink adımı olmalı (bölgeyi o bulur)",
                              "veya bench.toml [camera] roi = [x, y, w, h] ver"])
        roi, roi_src = sess.cam_last["roi"], "önceki cam_blink adımı"
    else:
        roi, cands = find_led_roi(small[use], scale)
        roi_src = "otomatik"
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
               header="t_s,parlaklik", comments="")
    mark = frames[int(np.argmax(np.where(use, sig_all, -1)))].copy()
    cv2.rectangle(mark, (x, y), (x + rw, y + rh), (0, 0, 255), 3)
    for c in cands[1:]:
        cv2.circle(mark, (c["x"], c["y"]), 20, (0, 255, 255), 2)

    def save_mark(label: str):
        cv2.putText(mark, label, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)
        cv2.imwrite(str(run_dir / f"{base}_roi.jpg"), mark)

    where = f"bölge {roi} ({roi_src}), {len(tt)} kare, {fps:.1f} fps"
    files = f"video {video.name}, işaretli kare {base}_roi.jpg"

    if steady:
        prev = sess.cam_last
        thr = prev["threshold"] if prev and roi == prev["roi"] else None
        if thr is None:
            raise BenchError("plan_invalid", "steady kontrolü için açık/kapalı eşiği bilinmiyor.",
                             ["Aynı bölgede önce yanıp sönen bir cam_blink adımı olmalı"])
        hyst = 0.1 * prev["contrast"]
        rising, falling = blink_edges(tt, sig, thr, hyst)
        level = float(np.median(sig))
        is_on = level > thr
        save_mark(f"steady {'ON' if is_on else 'OFF'}? level {level:.0f} thr {thr:.0f}")
        msg = (f"seviye {level:.0f} (eşik {thr:.0f}, önceki açık/kapalı {prev['hi']:.0f}/{prev['lo']:.0f}), "
               f"{len(rising) + len(falling)} geçiş; {where}; {files}")
        if rising or falling:
            raise BenchError("value_out_of_range", f"LED sabit olmalıydı ama {len(rising) + len(falling)} geçiş var: {msg}",
                             ["LED hâlâ yanıp sönüyor (heartbeat kapanmadı)", "Kamerada titreşim veya ışık değişimi"])
        if is_on != (steady == "on"):
            raise BenchError("value_out_of_range",
                             f"LED sabit {'AÇIK' if steady == 'on' else 'KAPALI'} olmalıydı, "
                             f"ölçülen {'açık' if is_on else 'kapalı'}: {msg}",
                             ["LED komutu uygulanmadı", "LED bölgesi yanlış (işaretli kareye bak)"])
        return f"LED sabit {'açık' if is_on else 'kapalı'}: {msg}"

    if contrast < CAM_MIN_CONTRAST:
        save_mark(f"no blink, contrast {contrast:.0f}")
        raise BenchError("no_blink_detected",
                         f"LED bölgesinde yanıp sönme yok (kontrast {contrast:.0f} < {CAM_MIN_CONTRAST}); {where}; {files}.",
                         ["LED yanıp sönmüyor (kart donmuş veya LED sabit)", "Telefon karta bakmıyor (işaretli kareye bak)",
                          "LED bölgesi yanlış: bench.toml [camera] roi ile sabitle"])
    thr = (lo_lvl + hi_lvl) / 2
    rising, falling = blink_edges(tt, sig, thr, 0.1 * contrast)
    fr, ff = edge_freq(rising), edge_freq(falling)
    if fr is None and ff is None:
        save_mark(f"edges {len(rising)}/{len(falling)}")
        raise BenchError("no_blink_detected",
                         f"Frekans için yeterli geçiş yok (yükselen {len(rising)}, düşen {len(falling)}); {where}; {files}.",
                         ["Kayıt süresi çok kısa: seconds'u artır", "LED çok yavaş yanıp sönüyor"])
    # İki kenar türünün periyot sayısıyla ağırlıklı ortalaması
    n_r, n_f = max(len(rising) - 1, 0), max(len(falling) - 1, 0)
    freq = ((fr or 0) * n_r + (ff or 0) * n_f) / (n_r + n_f)
    # Doluluk: her yükselen kenardan sonraki düşen kenara kadar açık süre / periyot
    ons = [next(f for f in falling if f > r) - r for r in rising if any(f > r for f in falling)]
    duty = float(np.mean(ons)) * freq if ons else None
    sess.cam_last = {"roi": roi, "threshold": thr, "contrast": contrast, "hi": hi_lvl, "lo": lo_lvl}
    save_mark(f"{freq:.3f} Hz duty {duty:.2f}" if duty is not None else f"{freq:.3f} Hz")

    lo, hi = step["range"]
    duty_txt = f"doluluk {duty:.2f}" if duty is not None else "doluluk ölçülemedi"
    msg = (f"{freq:.3f} Hz, {duty_txt} (yükselen {len(rising)}, düşen {len(falling)} kenar, "
           f"kontrast {contrast:.0f}); {where}; {files}")
    if cands:
        msg += "; adaylar " + ", ".join(f"({c['x']},{c['y']}) std {c['std']}" for c in cands)
    warn = ""
    if freq > fps / 4:
        warn = (f"UYARI: frekans kare hızının dörtte birinden ({fps / 4:.1f} Hz) büyük; "
                f"periyot başına {fps / freq:.1f} kare, ölçüm güvenilmez. ")
    if not lo <= freq <= hi:
        raise BenchError("value_out_of_range", f"{warn}frekans {freq:.3f} Hz beklenen aralık [{lo}, {hi}] dışında: {msg}",
                         ["Firmware'in yanıp sönme hızı veya saati yanlış",
                          "Yanlış bölge ölçülüyor: işaretli kareye bak, gerekirse bench.toml [camera] roi",
                          "Kare hızı yetersiz (örnekleme hatası)"])
    drng = step.get("duty_range")
    if drng and (duty is None or not drng[0] <= duty <= drng[1]):
        raise BenchError("value_out_of_range", f"{warn}doluluk {duty_txt}, beklenen [{drng[0]}, {drng[1]}]: {msg}",
                         ["LED açık/kapalı süreleri firmware'de farklı", "Kamera pozlama süresi açık süreyi uzatıyor"])
    return f"{warn}{msg} ([{lo}, {hi}] Hz aralığında)"


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
    """Son 'beklenti' adımını asla eşleşmeyecek hale getirir (negatif kontrol)."""
    plan = json.loads(json.dumps(plan))
    for i in range(len(plan["step"]) - 1, -1, -1):
        s = plan["step"][i]
        if s["action"] == "clock_check":
            s["range"] = [-2.0, -1.0]  # oran asla negatif olamaz
            s["seconds"] = min(float(s["seconds"]), 1.0)
            return plan, i + 1
        if s["action"] == "cam_blink":
            if s.get("steady"):
                s["steady"] = "off" if s["steady"] == "on" else "on"  # tersini bekle
            else:
                s["range"] = [-2.0, -1.0]  # frekans asla negatif olamaz
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
    """ST-Link seri numarası: bench.toml'dan, yoksa Linux'ta sysfs'ten."""
    if cfg.get("probe", "backend") == "sim":
        return "sim"
    if cfg.get("probe", "serial_number"):
        return str(cfg.get("probe", "serial_number"))
    if IS_WIN or not Path("/sys/bus/usb/devices").exists():
        return "bilinmiyor (bench.toml [probe] serial_number ile ver)"
    found = []
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            if (d / "idVendor").read_text().strip() != "0483" or \
                    (d / "idProduct").read_text().strip() not in STLINK_PIDS:
                continue
            raw = (d / "serial").read_text(encoding="utf-8", errors="replace").strip("\n")
        except OSError:
            continue
        # ST-Link V2 seri numarasını ham bayt olarak verir; OpenOCD gibi onaltılık yaz
        if not raw.isalnum() and all(ord(c) < 256 for c in raw):
            raw = "".join(f"{ord(c):02X}" for c in raw)
        found.append(raw)
    if not found:
        return "bulunamadı"
    return ", ".join(sorted(found)) + (" (birden fazla ST-Link takılı)" if len(found) > 1 else "")


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
    """Önce debug portundan (mem 0x1FFF7A10), olmazsa seri porttaki UID cevabından."""
    seen = next((txt.split()[1] for _, d, txt in reversed(sess.transcript)
                 if d == "<" and re.fullmatch(r"UID [0-9A-F]{24}", txt)), None)
    try:
        uid = prog.read_uid()
        src = f"debug portu, mem 0x{UID_ADDR:08X}"
        if seen:
            src += ", seri porttaki UID ile aynı" if seen == uid else f", UYARI: seri port farklı ({seen})"
        return {"uid": uid, "source": src}
    except BenchError as e:
        if seen:
            return {"uid": seen, "source": "seri port UID komutu"}
        return {"uid": None, "source": f"okunamadı: {e.summary}"}


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
        "summary": (f"{name}: {len(results)}/{len(plan['step'])} adım geçti." if ok else
                    f"{name}: {results[-1]['step']}. adımda başarısız: {failed.summary}"),
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


AUDIT_PENDING = "- Denetim kaydı hash: (henüz yazılmadı)"


def write_report_md(run_dir: Path, r: dict, plan: dict) -> None:
    m = r["meta"]
    L = [f"# Test raporu: {r['plan']}", "",
         f"- Sonuç: **{'PASS' if r['ok'] else 'FAIL'}**",
         f"- Başlangıç: {m['started'].replace('T', ' ')}",
         f"- Bitiş: {m['finished'].replace('T', ' ')}",
         f"- Süre: {r['duration_s']} sn",
         f"- Plan dosyası: `{r['plan_file']}`"]
    if m.get("operator"):
        L.insert(3, f"- Operatör: {m['operator']}")
    if plan.get("description"):
        L.append(f"- Açıklama: {plan['description']}")
    L += ["", "## Ortam", ""]
    if not m["firmware"]:
        L.append("- Firmware: bu planda yükleme yok")
    for fw in m["firmware"]:
        L.append(f"- Firmware: `{fw['path']}`")
        L.append(f"  - SHA-256: `{fw['sha256'] or 'dosya yok'}`")
    L += [f"- Kart UID: `{m['uid'] or '-'}` ({m['source']})",
          f"- ST-Link seri no: `{m['probe_serial']}`",
          f"- bench.py: {m['bench_version']} (dosya SHA-256 `{m['bench_sha256'][:16]}…`)",
          f"- {m['programmer']}: {m['programmer_version']}",
          f"- Sunucu: {m['host']}",
          AUDIT_PENDING]
    L += ["", "| # | Adım | Sonuç | Açıklama | Süre (sn) |", "|---|---|---|---|---|"]
    for s in r["steps"]:
        summ = str(s["summary"]).replace("|", "\\|")
        L.append(f"| {s['step']} | {s['name']} | {'PASS' if s['ok'] else 'FAIL'} | {summ} | {s['duration_s']} |")
    if not r["ok"]:
        L += ["", "## Hata", "", f"- Tür: `{r.get('error_type')}`"]
        for c in r.get("likely_causes", []):
            L.append(f"- Olası sebep: {c}")
        if r.get("details"):
            L += ["", "```", str(r["details"]), "```"]
    rec = r.get("recovery", {})
    if rec.get("attempted"):
        L += ["", "## Kurtarma", "", f"- Sonuç: {'kart kurtarıldı' if rec['ok'] else 'KURTARILAMADI, insan müdahalesi gerekli'}"]
        for a in rec["actions"]:
            L.append(f"- {a['action']}: {'OK' if a['ok'] else 'başarısız'} {a.get('line') or a.get('error') or ''}")
    L += ["", "Seri port dökümü: `serial.log`"]
    (run_dir / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------

def cmd_run(cfg, args, board) -> dict:
    plan_path, plan = load_plan(cfg, args.plan)
    r1 = execute_plan(cfg, plan_path, plan, board)
    if not args.verify_can_fail or not r1["ok"]:
        return r1
    mplan, mstep = mutate_plan(plan)
    if mstep is None:
        r1["negative_check"] = {"ok": False, "summary": "Planda beklenti adımı yok; negatif kontrol yapılamadı."}
        r1["ok"] = False
        r1["error_type"] = "test_cannot_fail"
        r1["summary"] += " Ama planda hiçbir beklenti yok, bu test hiçbir şeyi kanıtlamaz."
        return r1
    if board is not None:
        board.out.clear()
    r2 = execute_plan(cfg, plan_path, mplan, board, label="_negatif")
    neg = {"mutated_step": mstep, "report_dir": r2["report_dir"], "failed_as_expected": not r2["ok"],
           "recovery_ok": r2["recovery"].get("ok")}
    r1["negative_check"] = neg
    if r2["ok"]:
        r1.update(ok=False, error_type="test_cannot_fail",
                  summary=f"{r1['plan']}: bilerek bozulan beklentiyle de GEÇTİ. Bu test hiçbir şeyi ölçmüyor.",
                  likely_causes=["Tezgah başarısız bir beklentiyi FAIL olarak raporlamıyor (bench.py hatası)",
                                 "Bozulan adım hiç çalışmıyor"])
    else:
        r1["summary"] += f" Negatif kontrol: {mstep}. adım bozulunca beklendiği gibi FAIL verdi."
    return r1


def cmd_flash(cfg, args, board) -> dict:
    check_permission(cfg, "allow_flash", "Yükleme")
    image = Path(args.image)
    if not image.is_absolute() and not image.exists():
        image = cfg.resolve(image)  # planlardaki gibi: bulunamazsa bench.toml'un klasörüne göre
    if not image.is_file():
        raise BenchError("image_not_found", f"{image} bulunamadı.", ["Önce derle: python firmware/build.py"])
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
        raise BenchError("expect_timeout", f"Reset sonrası {args.timeout:g} sn içinde /{args.expect}/ gelmedi.",
                         ["Firmware açılışta çöküyor", "Seri bağlantı/baud hatası"],
                         "Gelen satırlar:\n" + ("\n".join(seen) or "(hiç yok)"))
    return ok_result("reset", f"Kart resetlendi, açılış: {line}", lines=seen)


def cmd_send(cfg, args, board) -> dict:
    sess = open_session(cfg, board)
    try:
        sess.drain()
        sess.send_line(args.text)
        if args.expect:
            m, line, seen = sess.wait_for(args.expect, args.timeout)
            if not m:
                if sess.last_fault:
                    raise BenchError("target_crashed", f"'{args.text}' komutundan sonra kart çöktü: {sess.last_fault}",
                                     ["pc'yi koda çevir: arm-none-eabi-addr2line -f -e firmware.elf <pc>",
                                      "Kartı geri getir: bench.py reset --expect \"^BOOT\""], sess.recent())
                raise BenchError("expect_timeout",
                                 f"'{args.text}' komutuna {args.timeout:g} sn içinde /{args.expect}/ cevabı gelmedi.",
                                 ["Kart daha önce çökmüş veya donmuş olabilir: 'bench.py reset --expect \"^BOOT\"' dene",
                                  "TX/RX kabloları ters veya GND bağlı değil"],
                                 "Son seri trafik:\n" + sess.recent())
            good, msg = check_range(m, [args.min, args.max] if args.min is not None else None)
            if not good:
                raise BenchError("value_out_of_range", f"'{line}': {msg}")
            return ok_result("send", f"'{args.text}' -> '{line}'", response=seen)
        lines = sess.collect(args.timeout)
        return ok_result("send", f"'{args.text}' gönderildi, {len(lines)} satır cevap.", response=lines)
    finally:
        sess.close()


def cmd_read(cfg, args, board) -> dict:
    sess = open_session(cfg, board)
    try:
        if args.until:
            m, line, seen = sess.wait_for(args.until, args.seconds)
            if not m:
                raise BenchError("expect_timeout", f"{args.seconds:g} sn içinde /{args.until}/ gelmedi.",
                                 [], "\n".join(seen) or "(hiç yok)")
            return ok_result("read", f"Bulundu: {line}", lines=seen)
        lines = sess.collect(args.seconds)
        return ok_result("read", f"{args.seconds:g} sn içinde {len(lines)} satır okundu.", lines=lines)
    finally:
        sess.close()


def cmd_recover(cfg, args, board) -> dict:
    check_permission(cfg, "allow_flash", "Yükleme")
    golden = cfg.resolve(cfg.get("recovery", "golden_image"))
    if not golden.is_file():
        raise BenchError("image_not_found", f"Sağlam firmware bulunamadı: {golden}",
                         ["python firmware/build.py ile derle", "bench.toml [recovery] golden_image yolunu kontrol et"])
    prog = make_programmer(cfg, board)
    sess = open_session(cfg, board)
    try:
        rec = recover_board(cfg, prog, sess)
    finally:
        sess.close()
    if not rec["ok"]:
        raise BenchError("recovery_failed", "Kart otomatik olarak kurtarılamadı.",
                         ["Kartın USB kablosunu çıkarıp tak, sonra tekrar dene",
                          "BOOT0'ı 1 yap, kartı resetle, tekrar dene (sistem bootloader'ı)",
                          "İnsan müdahalesi gerekli"], json.dumps(rec, ensure_ascii=False, indent=2))
    return ok_result("recover", "Kart kurtarıldı ve açılış mesajı görüldü.", recovery=rec)


# --- Hata ayıklama: sadece okuma (halt-dump, mem) ve insan için gdbserver ---

# STM32F407 için okunmasına izin verilen bölgeler. Option byte'lar (0x1FFFC000) bilerek dışarıda.
READ_REGIONS = [
    (0x08000000, 0x08100000, "flash"),
    (0x10000000, 0x10010000, "CCM RAM"),
    (0x1FFF0000, 0x1FFF7A30, "sistem belleği, OTP, UID"),
    (0x20000000, 0x20020000, "SRAM"),
    (0x40000000, 0x60000000, "çevre birimleri"),
    (0xE0000000, 0xE0100000, "Cortex-M çekirdek (SCB, NVIC, SysTick)"),
]
MEM_MAX_WORDS = 256
UID_ADDR = 0x1FFF7A10

SCB_REGS = ["cfsr", "hfsr", "dfsr", "mmfar", "bfar"]  # 0xE000ED28'den itibaren ardışık
CFSR_BITS = {
    0: "IACCVIOL: yasak adresten komut çalıştırma (MPU)",
    1: "DACCVIOL: yasak adrese veri erişimi (MPU)",
    3: "MUNSTKERR: istisna dönüşünde yığın okuma hatası (MPU)",
    4: "MSTKERR: istisna girişinde yığına yazma hatası (MPU)",
    5: "MLSPERR: FPU durumu kaydedilirken hata (MPU)",
    7: "MMARVALID: MMFAR geçerli",
    8: "IBUSERR: komut okurken bus hatası",
    9: "PRECISERR: veri erişiminde bus hatası, adres BFAR'da",
    10: "IMPRECISERR: kesin olmayan bus hatası (pc hatalı komuttan sonrasını gösterebilir)",
    11: "UNSTKERR: istisna dönüşünde yığın okuma bus hatası",
    12: "STKERR: istisna girişinde yığın taşması / bus hatası",
    13: "LSPERR: FPU durumu kaydedilirken bus hatası",
    15: "BFARVALID: BFAR geçerli",
    16: "UNDEFINSTR: tanımsız komut (udf, bozuk kod, yanlış adrese atlama)",
    17: "INVSTATE: geçersiz durum (Thumb biti 0 olan adrese atlama, ör. NULL fonksiyon işaretçisi)",
    18: "INVPC: geçersiz EXC_RETURN ile dönüş",
    19: "NOCP: kapalı yardımcı işlemci (FPU açılmadan float kullanımı)",
    24: "UNALIGNED: hizasız erişim",
    25: "DIVBYZERO: sıfıra bölme",
}
HFSR_BITS = {1: "VECTTBL: vektör tablosu okunamadı",
             30: "FORCED: yapılandırılabilir bir hata HardFault'a yükseltildi (CFSR'ye bak)",
             31: "DEBUGEVT: debug olayı"}
EXC_NAMES = {0: "thread (normal kod)", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
             6: "UsageFault", 11: "SVCall", 14: "PendSV", 15: "SysTick"}


def parse_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise BenchError("bad_argument", f"Sayı anlaşılamadı: {text!r}", ["0x ile onaltılık veya ondalık yaz"])


def is_code_addr(v: int) -> bool:
    return 0x08000000 <= v < 0x08100000


def parse_mdw(out: str) -> list[tuple[int, int]]:
    """OpenOCD 'mdw' çıktısını (adres, değer) listesine çevirir."""
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
    """Çekirdeği durdurur, register'ları, hata register'larını ve yığını okur, sonra devam ettirir."""
    prog = make_programmer(cfg, board)
    n_stack = 48
    out = prog.debug_commands([
        "halt",
        "echo @@REGS", "reg",
        "echo @@SCB", "mdw 0xE000ED28 5",
        # SRAM sonunu (0x20020000) aşmadan oku; sp bozuksa hata versin ama gerisi devam etsin
        "echo @@STACK",
        "set _sp [lindex [regexp -inline {0x[0-9a-fA-F]+} [reg sp]] 0]; "
        f"set _n {n_stack}; "
        "if {$_sp >= 0x20000000 && $_sp < 0x20020000 && (0x20020000 - $_sp) / 4 < $_n} "
        "{ set _n [expr {(0x20020000 - $_sp) / 4}] }; "
        # catch içindeki çıktı ekrana basılmaz, sonuç olarak döner; echo ile basıyoruz
        "if {$_n > 0 && [catch { mdw $_sp $_n } _r] == 0} { echo $_r }",
        *([] if args.keep_halted else ["resume"]),
    ])
    sections = dict(re.findall(r"@@(\w+)\n(.*?)(?=@@\w+\n|\Z)", out, re.S))
    regs = parse_regs(sections.get("REGS", ""))
    if "pc" not in regs:
        raise BenchError("tool_error", "Register'lar okunamadı.", ["Çekirdek durdurulamamış olabilir"], tail(out))
    scb = dict(zip(SCB_REGS, (v for _, v in parse_mdw(sections.get("SCB", "")))))
    # Adres register'ları sadece CFSR'deki VALID biti 1 ise anlamlı
    if not scb.get("cfsr", 0) & (1 << 7):
        scb.pop("mmfar", None)
    if not scb.get("cfsr", 0) & (1 << 15):
        scb.pop("bfar", None)
    stack = parse_mdw(sections.get("STACK", ""))

    elf = symbols_elf(cfg, args)
    exc = regs.get("xPSR", 0) & 0x1FF

    # İstisna içindeysek yığında EXC_RETURN (0xFFFFFFxx) arayıp donanımın kaydettiği çerçeveyi bul:
    # hemen arkasından r0 r1 r2 r3 r12 lr pc xpsr gelir (handler LR'yi en son itip sakladıysa).
    frame = None
    if exc:
        for i, (_, v) in enumerate(stack):
            if v & 0xFFFFFFE0 == 0xFFFFFFE0 and v & 0x4 == 0 and i + 8 < len(stack):
                f = [w for _, w in stack[i + 1:i + 9]]
                if is_code_addr(f[6]):
                    frame = dict(zip(["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"], f))
                    frame["found_at"] = stack[i + 1][0]
                    break

    candidates = [v for _, v in stack if is_code_addr(v) and v & 1]  # Thumb dönüş adresleri
    want = [regs["pc"], regs.get("lr", 0) & ~1] + ([frame["pc"], frame["lr"] & ~1] if frame else []) + \
           [(v & ~1) - 2 for v in candidates]  # dönüş adresinin 2 byte gerisi = çağıran komut
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

    where = syms.get(regs["pc"], "sembol yok")
    exc_name = EXC_NAMES.get(exc, f"IRQ{exc - 16}" if exc >= 16 else f"istisna {exc}")
    summary = f"Çekirdek {exc_name} içinde, pc={h(regs['pc'])} ({where})."
    if frame:
        summary += f" Çökme anı: pc={h(frame['pc'])} ({syms.get(frame['pc'], 'sembol yok')})."
    summary += " Çekirdek durdurulmuş halde bırakıldı." if args.keep_halted else " Çekirdek devam ettirildi."

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
            print(f"  Kaydedilmiş çerçeve: pc={h(frame['pc'])} {r['stacked_frame']['pc_at'] or ''}"
                  f"  lr={h(frame['lr'])} {r['stacked_frame']['lr_at'] or ''}")
        if chain:
            print("  Yığındaki kod adresleri (olası çağrı zinciri, yukarıdan aşağı):")
            for c in chain:
                print(f"    {c['value']}  {c['at']}")
        print(f"  Semboller: {elf}  (kartta başka firmware varsa isimler yanlış olur)")
    return r


def cmd_mem(cfg, args, board) -> dict:
    addr = parse_int(args.addr)
    count = args.count
    if addr % 4:
        raise BenchError("bad_argument", f"Adres 4'e bölünmeli: 0x{addr:08X}")
    if not 1 <= count <= MEM_MAX_WORDS:
        raise BenchError("bad_argument", f"--count 1 ile {MEM_MAX_WORDS} arasında olmalı.")
    end = addr + 4 * count
    region = next((r for r in READ_REGIONS if r[0] <= addr and end <= r[1]), None)
    if not region:
        raise BenchError("permission_denied", f"0x{addr:08X}-0x{end - 1:08X} izin verilen bölgelerin dışında.",
                         ["İzinli: " + ", ".join(f"{n} 0x{a:08X}-0x{b - 1:08X}" for a, b, n in READ_REGIONS)])
    prog = make_programmer(cfg, board)
    # Çekirdek durdurulmaz; ST-Link çalışırken de belleği okuyabilir.
    words = parse_mdw(prog.debug_commands([f"mdw 0x{addr:08X} {count}"]))
    if len(words) != count:
        raise BenchError("tool_error", f"{count} kelime istendi, {len(words)} okundu.",
                         ["Bu adreste bellek yok (bus hatası); STM32F407 referans kılavuzundaki bellek haritasına bak"])
    if not args.json:
        for i in range(0, len(words), 4):
            row = words[i:i + 4]
            print(f"  0x{row[0][0]:08X}:  " + " ".join(f"{v:08X}" for _, v in row))
    return ok_result("mem", f"0x{addr:08X} adresinden {count} kelime okundu ({region[2]}).",
                     words=[{"addr": f"0x{a:08X}", "value": f"0x{v:08X}"} for a, v in words])


def cmd_gdbserver(cfg, args, board) -> dict:
    """İnsan için: OpenOCD GDB sunucusunu açar, Ctrl+C'ye kadar tezgahı kilitli tutar."""
    prog = make_programmer(cfg, board)
    if not hasattr(prog, "gdb_server_cmd"):
        raise BenchError("not_supported", "gdbserver sadece openocd backend'inde var.")
    elf = symbols_elf(cfg, args)
    print(f"GDB sunucusu localhost:{args.port} üzerinde açılıyor. Kapatmak için Ctrl+C.")
    print(f"Başka bir terminalde:  gdb-multiarch {elf} -ex 'target extended-remote :{args.port}'")
    print("(gdb-multiarch yoksa: sudo apt install gdb-multiarch)\n", flush=True)
    p = subprocess.Popen(prog.gdb_server_cmd(args.port))
    try:
        rc = p.wait()
    except KeyboardInterrupt:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        return ok_result("gdbserver", "GDB sunucusu kapatıldı.")
    if rc != 0:
        raise BenchError("tool_error", f"openocd hata kodu {rc}", ["Yukarıdaki openocd çıktısına bak"])
    return ok_result("gdbserver", "GDB sunucusu kapandı.")


def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        raise BenchError("pyserial_missing", "pyserial kurulu değil.",
                         ["Windows: py -m pip install pyserial", "Linux: sudo apt install python3-serial"])
    return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in lp.comports()]


def cmd_ports(cfg, args, board) -> dict:
    ports = list_ports()
    if not args.json:
        if not ports:
            print("Hiç seri port bulunamadı. USB-TTL takılı mı?")
        for p in ports:
            print(f"  {p['device']:<16} {p['description']}  [{p['hwid']}]")
        if not IS_WIN and Path("/dev/serial/by-id").exists():
            print("\nKalıcı isimler (bench.toml için önerilir):")
            for p in sorted(Path("/dev/serial/by-id").iterdir()):
                print(f"  {p}  ->  {os.path.realpath(p)}")
    return ok_result("ports", f"{len(ports)} seri port bulundu.", ports=ports)


def cmd_doctor(cfg, args, board) -> dict:
    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"check": name, "ok": ok, "detail": detail, "required": required})

    add("python", True, sys.version.split()[0])
    try:
        import serial
        add("pyserial", True, serial.__version__)
    except ImportError:
        add("pyserial", False, "kurulu değil: py -m pip install pyserial")
    add("config", True, str(cfg.path) if cfg.path else "(varsayılanlar, --sim)")

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
        add("serial", False, "bench.toml [serial] port boş")
    else:
        try:
            devs = {p["device"] for p in list_ports()}
            real = os.path.realpath(port) if not IS_WIN else port
            found = port in devs or real in devs or (not IS_WIN and Path(port).exists())
            add("serial", found, f"{port} {'bulundu' if found else 'listede yok (bench.py ports)'}")
        except BenchError as e:
            add("serial", False, e.summary)

    golden = cfg.resolve(cfg.get("recovery", "golden_image"))
    add("golden_image", golden.is_file(), str(golden) if golden.is_file() else
        f"{golden} yok: python firmware/build.py", required=False)

    gcc = shutil.which("arm-none-eabi-gcc") or find_tool(None, [], [
        r"C:\ST\STM32CubeCLT*\GNU-tools-for-STM32\bin\arm-none-eabi-gcc.exe",
        r"C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
        r"C:\Program Files\Arm GNU Toolchain arm-none-eabi\*\bin\arm-none-eabi-gcc.exe",
        "/opt/st/stm32cubeclt*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc"])
    add("arm-none-eabi-gcc", bool(gcc), gcc or "bulunamadı (firmware derlemek için gerekli)", required=False)

    ok = all(c["ok"] for c in checks if c["required"])
    if not args.json:
        for c in checks:
            mark = "OK  " if c["ok"] else ("HATA" if c["required"] else "UYARI")
            print(f"  [{mark}] {c['check']:<18} {c['detail']}")
        print()
    return {"ok": ok, "command": "doctor", "checks": checks,
            "summary": "Tezgah hazır." if ok else "Eksikler var, yukarıdaki HATA satırlarına bak.",
            **({} if ok else {"error_type": "not_ready"})}


def cmd_monitor(cfg, args, board) -> dict:
    sess = open_session(cfg, board)
    print(f"Seri monitör ({cfg.get('serial', 'port')}). Çıkmak için Ctrl+C.")
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
    return ok_result("monitor", "Monitör kapandı.")


def cmd_audit(cfg, args, board) -> dict:
    return audit_verify(cfg)


# ---------------------------------------------------------------------------
# Üretim istasyonu: kart kart programla ve son test
# ---------------------------------------------------------------------------

STATION_CSV_FIELDS = ["zaman", "kart_no", "operator", "uid", "firmware_sha256", "sonuc", "basarisiz_adim",
                      "rapor_klasoru", "tekrar", "denetim_hash"]

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
    """Sahte kartla yapılan denemeler gerçek üretim kaydına karışmasın."""
    return "_sim" if cfg.get("probe", "backend") == "sim" else ""


def station_previous(cfg: Config, uid: str) -> list[str]:
    """Bu UID'nin daha önceki istasyon kayıtlarının zamanları (aynı türdeki tüm station_*.csv dosyalarından)."""
    found = []
    sim = station_csv_kind(cfg) == "_sim"
    for f in sorted(cfg.runs_dir.glob("station_*.csv")):
        if f.stem.endswith("_sim") != sim:
            continue
        try:
            with f.open(encoding="utf-8", newline="") as fh:
                found += [row["zaman"] for row in csv.DictReader(fh) if row.get("uid") == uid]
        except (OSError, KeyError, csv.Error):
            continue
    return found


def station_csv_append(cfg: Config, row: dict) -> Path:
    path = cfg.runs_dir / f"station_{dt.date.today():%Y%m%d}{station_csv_kind(cfg)}.csv"
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=STATION_CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return path


def station_card(cfg: Config, plans: list, board, operator: str, card_no: int) -> dict:
    """Bir kart: planları sırayla çalıştırır, ilk başarısız planda durur. Denetim satırını yazar."""
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
        failed = (f"{bad['plan']}: {st['step']}. adım {st['name']} ({st['error_type']})" if st
                  else f"{bad['plan']}: {bad.get('error_type')}")
    prev = station_previous(cfg, uid) if uid else []
    dirs = [r["report_dir"] for r in results]
    h = audit_append(cfg, {"cmd": "station-card", "card_no": card_no, "operator": operator, "uid": uid,
                           "firmware_sha256": fw, "ok": ok, "failed_step": failed or None,
                           "report_dirs": dirs, "repeat_of": prev or None})
    for r in results:
        stamp_audit_hash(r, h)
    row = {"zaman": dt.datetime.now().isoformat(timespec="seconds"), "kart_no": card_no, "operator": operator,
           "uid": uid or "okunamadı", "firmware_sha256": fw, "sonuc": "PASS" if ok else "FAIL",
           "basarisiz_adim": failed, "rapor_klasoru": ";".join(dirs),
           "tekrar": f"TEKRAR: {len(prev)} önceki test, ilki {prev[0]}" if prev else "", "denetim_hash": h}
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
        raise BenchError("invalid_args", "--no-prompt ile --rounds N de verilmeli (yoksa döngü bitmez).")
    names = [n.strip() for n in args.plans.split(",") if n.strip()]
    plans = [load_plan(cfg, n) for n in names]  # hepsi başta doğrulanır

    operator = (args.operator or "").strip()
    if not operator:
        if args.no_prompt:
            raise BenchError("invalid_args", "--no-prompt ile --operator ADI verilmeli (izlenebilirlik için).")
        while not operator:
            operator = input("Operatör adı: ").strip()
    say(f"İstasyon: {cfg.get('bench', 'name')} | operatör: {operator} | planlar: {', '.join(names)}")

    cards = []
    interrupted = False
    try:
        while not args.rounds or len(cards) < args.rounds:
            n = len(cards) + 1
            if not args.no_prompt:
                try:
                    ans = input(f"\n[{n}] Kartı tak, Enter'a bas (çıkmak için q): ").strip().lower()
                except EOFError:
                    break
                if ans == "q":
                    break
            else:
                say(f"\n[{n}] kart test ediliyor...")
            c = station_card(cfg, plans, board, operator, n)
            cards.append(c)
            say(big_text("PASS" if c["ok"] else "FAIL", color))
            say(f"  UID {c['uid']}  firmware {c['firmware_sha256'][:16]}…")
            for pr in c["plans"]:
                say(f"  [{'PASS' if pr['ok'] else 'FAIL'}] {pr['summary']}")
            if c["basarisiz_adim"]:
                say(f"  Başarısız adım: {c['basarisiz_adim']}")
            if c["repeat_count"]:
                say(f"  !!! BU KART DAHA ÖNCE TEST EDİLDİ: {c['tekrar']}")
    except KeyboardInterrupt:
        interrupted = True
        say("\nDurduruldu.")

    n_pass = sum(c["ok"] for c in cards)
    n_rep = sum(1 for c in cards if c["repeat_count"])
    summary = (f"İstasyon: {len(cards)} kart, {n_pass} PASS, {len(cards) - n_pass} FAIL"
               + (f", {n_rep} tekrar test" if n_rep else "") + (" (durduruldu)" if interrupted else "") + ".")
    res = ok_result("station", summary, operator=operator, plans=names, cards=cards,
                    csv=cards[-1]["csv"] if cards else None)
    if len(cards) != n_pass:
        res.update(ok=False, error_type="cards_failed",
                   likely_causes=["Başarısız kartların rapor klasörlerine ve serial.log'larına bak"])
    return res


COMMANDS = {
    "doctor": (cmd_doctor, False), "ports": (cmd_ports, False), "flash": (cmd_flash, True),
    "reset": (cmd_reset, True), "send": (cmd_send, True), "read": (cmd_read, True),
    "run": (cmd_run, True), "recover": (cmd_recover, True), "monitor": (cmd_monitor, True),
    "audit": (cmd_audit, False), "halt-dump": (cmd_halt_dump, True), "mem": (cmd_mem, True),
    "gdbserver": (cmd_gdbserver, True), "station": (cmd_station, True),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench.py", description="STM32 test tezgahı aracı (Windows + Linux)")
    p.add_argument("--config", help="bench.toml yolu (varsayılan: bench.py'nin yanındaki)")
    p.add_argument("--json", action="store_true", help="Sonucu JSON olarak yaz (ajanlar için)")
    p.add_argument("--sim", action="store_true", help="Donanım olmadan sahte kartla çalış")
    p.add_argument("--version", action="version", version=f"stm32bench {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="Kurulumu ve bağlantıları kontrol et")
    sub.add_parser("ports", help="Seri portları listele")

    s = sub.add_parser("flash", help="Firmware yükle (.elf/.hex/.bin)")
    s.add_argument("image")
    s.add_argument("--under-reset", action="store_true", help="Reset altında bağlan (kilitlenmiş kart için)")

    s = sub.add_parser("reset", help="Kartı resetle")
    s.add_argument("--expect", help="Reset sonrası beklenen satır (regex), ör. '^BOOT'")
    s.add_argument("--timeout", type=float, default=3)

    s = sub.add_parser("send", help="Seri porttan komut gönder")
    s.add_argument("text")
    s.add_argument("--expect", help="Beklenen cevap (regex)")
    s.add_argument("--timeout", type=float, default=2)
    s.add_argument("--min", type=float, help="Yakalanan sayının alt sınırı")
    s.add_argument("--max", type=float, help="Yakalanan sayının üst sınırı")

    s = sub.add_parser("read", help="Seri porttan oku")
    s.add_argument("--seconds", type=float, default=2)
    s.add_argument("--until", help="Bu desene uyan satır gelince dur (regex)")

    s = sub.add_parser("run", help="Test planı çalıştır (plans/<isim>.toml)")
    s.add_argument("plan")
    s.add_argument("--verify-can-fail", action="store_true",
                   help="Geçerse bir de bilerek bozulmuş beklentiyle çalıştır; FAIL vermeli")

    sub.add_parser("recover", help="Sağlam firmware'i reset altında yükleyip kartı kurtar")

    s = sub.add_parser("station", help="Üretim istasyonu: kart kart programla ve son test (q ile çık)")
    s.add_argument("--plans", default="smoke,measure", help="Virgülle ayrılmış planlar (varsayılan: smoke,measure)")
    s.add_argument("--operator", help="Operatör adı (verilmezse başta sorulur)")
    s.add_argument("--rounds", type=int, default=0, help="Bu kadar karttan sonra dur (0: q'ya kadar)")
    s.add_argument("--no-prompt", action="store_true", help="Enter bekleme (test için; --rounds ve --operator gerekir)")

    s = sub.add_parser("monitor", help="Canlı seri monitör (insan için)")
    s.add_argument("--seconds", type=float, default=0)

    sub.add_parser("audit", help="Denetim kaydının bütünlüğünü kontrol et")

    s = sub.add_parser("halt-dump", help="Çekirdeği durdur; register, hata register'ları ve yığını oku, devam ettir")
    s.add_argument("--keep-halted", action="store_true", help="Okuduktan sonra çekirdeği durdurulmuş bırak")
    s.add_argument("--elf", help="Semboller için ELF (varsayılan: [recovery] golden_image)")

    s = sub.add_parser("mem", help="Bellek/register oku (sadece okuma, izinli bölgeler)")
    s.add_argument("addr", help="Başlangıç adresi, ör. 0x40020C14")
    s.add_argument("--count", type=int, default=1, help=f"Kaç 32-bit kelime (en fazla {MEM_MAX_WORDS})")

    s = sub.add_parser("gdbserver", help="GDB sunucusu aç (sadece insan için, Ctrl+C ile kapanır)")
    s.add_argument("--port", type=int, default=3333)
    s.add_argument("--elf", help="gdb'ye önerilecek ELF (varsayılan: [recovery] golden_image)")
    return p


def stamp_audit_hash(result: dict, h: str) -> None:
    """Denetim satırı rapordan sonra yazılır; hash'ini rapora ve result.json'a sonradan işle."""
    dirs = [result.get("report_dir"), (result.get("negative_check") or {}).get("report_dir")]
    for d in filter(None, dirs):
        rep = Path(d) / "report.md"
        if rep.exists():
            rep.write_text(rep.read_text(encoding="utf-8").replace(
                AUDIT_PENDING, f"- Denetim kaydı hash: `{h}` (runs/audit.jsonl)"), encoding="utf-8")
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
        result = {"ok": False, "command": args.cmd, "error_type": "interrupted", "summary": "Kullanıcı durdurdu."}

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
            print(f"  Kurtarma: {'başarılı' if rec.get('ok') else 'BAŞARISIZ, insan müdahalesi gerekli'}")
        neg = result.get("negative_check")
        if neg:
            print(f"  Negatif kontrol: {'FAIL verdi (iyi)' if neg.get('failed_as_expected') else 'GEÇTİ (kötü)'}")
    emit(result, args.json)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
