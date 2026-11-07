"""
Host-side checks for firmware/boot_c0 (NUCLEO-C031C6 port, no hardware):

    python -m unittest discover -s firmware/boot_c0/tests -v

Builds firmware/boot_c0/build/ first if it is missing (needs arm-none-eabi-gcc), then checks image
headers (256 B, CRC at 252), CRCs, link addresses, the bad images (wrong_board = the real F407 v2),
the 32 KB golden layout, the manifest, and static properties of the ELFs: ARMv6-M, RAM functions
and ISRs really live in RAM and never call/branch into flash, vector table entries (TIM3/TIM14),
noinit placement, status line strings with target=c031, the flash driver's page guard and the
absence of mass erase / option byte writes, and pointer casts that could be unaligned.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

BOOT = Path(__file__).resolve().parents[1]
F407 = BOOT.parent / "boot"
BUILD = BOOT / "build"
IMAGES = BUILD / "images"
sys.path.insert(0, str(BOOT))
import mkimage as mk  # noqa: E402

OBJDUMP = shutil.which("arm-none-eabi-objdump")
NM = shutil.which("arm-none-eabi-nm")
READELF = shutil.which("arm-none-eabi-readelf")

RAM_LO, RAM_HI = 0x20000000, 0x20003000
GOLDEN = 0x8000


def setUpModule():
    if not (IMAGES / "manifest.json").exists():
        if not shutil.which("arm-none-eabi-gcc"):
            raise unittest.SkipTest("no build and no arm-none-eabi-gcc")
        p = subprocess.run([sys.executable, str(BOOT / "build_boot.py"), "--json"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("build_boot.py failed:\n" + p.stdout + p.stderr)


def img(name: str) -> bytes:
    return (IMAGES / name).read_bytes()


def split(data: bytes) -> tuple[dict, bytes, bytes]:
    return mk.parse_header(data[:mk.HDR_SIZE]), data[:mk.HDR_SIZE], data[mk.HDR_SIZE:]


def header_crc_ok(raw: bytes) -> bool:
    return zlib.crc32(raw[:252]) & 0xFFFFFFFF == struct.unpack_from("<I", raw, 252)[0]


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


class TestLayout(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(mk.BL_BASE, 0x08000000)
        self.assertEqual(mk.BL_SIZE, 12 * 1024)
        self.assertEqual(mk.META0_BASE, 0x08003000)
        self.assertEqual(mk.META1_BASE, 0x08003800)
        self.assertEqual(mk.SLOT_BASE, {"A": 0x08004000, "B": 0x08006000})
        self.assertEqual(mk.SLOT_SIZE, 8192)
        self.assertEqual(mk.HDR_SIZE, 256)
        self.assertEqual(mk.HDR_CRC_OFF, 252)
        self.assertEqual(mk.PAYLOAD_MAX, 8192 - 256)
        self.assertEqual(mk.PROG_UNIT, 8)
        self.assertEqual(mk.PAGE_SIZE, 2048)
        self.assertEqual(mk.GOLDEN_SIZE, GOLDEN)
        self.assertEqual(mk.IMG_MAGIC, 0x4D495A54)
        self.assertEqual(mk.BOARD_ID, 0xC031C600)
        self.assertEqual(mk.META_MAGIC, 0x4154454D)
        self.assertEqual(mk.TARGET, "c031")
        lay = (BOOT / "layout.h").read_text()
        for name, val in (("META0_PAGE", "6u"), ("META1_PAGE", "7u"), ("SLOT_A_PAGE", "8u"),
                          ("SLOT_B_PAGE", "12u"), ("SLOT_PAGES", "4u"), ("RAM_SIZE", "0x3000u")):
            self.assertRegex(lay, rf"#define\s+{name}\s+{val}")

    def test_link_script(self):
        ld = (BOOT / "link.ld.in").read_text()
        self.assertRegex(ld, r"NOINIT\s*\(rwx\)\s*:\s*ORIGIN = 0x20000000, LENGTH = 256")
        self.assertRegex(ld, r"RAM\s*\(rwx\)\s*:\s*ORIGIN = 0x20000100, LENGTH = 12K - 256")
        self.assertIn("_estack = 0x20003000;", ld)
        self.assertIn("ALIGN(256)", ld)
        self.assertIn("0x4E4F494E", (BOOT / "noinit.h").read_text())

    def test_portable_sources_identical_to_f407(self):
        """crc32/sha256/b64 are copies of firmware/boot (shared code, SPEC-C031C6 10)."""
        for f in ("crc32.c", "sha256.c", "b64.c"):
            self.assertEqual((BOOT / f).read_bytes(), (F407 / f).read_bytes(), f)

    def test_build_flags(self):
        src = (BOOT / "build_boot.py").read_text()
        for flag in ("-mcpu=cortex-m0plus", "-mthumb", "-mno-unaligned-access", "-Werror", "-lgcc"):
            self.assertIn(f'"{flag}"', src)
        self.assertNotIn("cortex-m4", src)
        self.assertNotIn("-mfloat-abi", src)


class TestAppImages(unittest.TestCase):
    def test_every_app_image(self):
        for var, m in mk.APPS.items():
            for slot in mk.SLOTS:
                with self.subTest(var=var, slot=slot):
                    data = img(f"{var}_{slot}.img")
                    h, raw, payload = split(data)
                    self.assertEqual(h["magic"], mk.IMG_MAGIC)
                    self.assertEqual(h["header_version"], 1)
                    self.assertEqual(h["flags"], 0)
                    self.assertEqual(h["board_id"], 0xC031C600)
                    self.assertEqual(h["fw_version"], mk.version_word(m["version"]))
                    self.assertEqual(h["security_counter"], m["sc"])
                    self.assertEqual(h["link_addr"], mk.SLOT_BASE[slot] + 0x100)
                    self.assertEqual(h["payload_size"], len(payload))
                    self.assertEqual(len(payload) % 8, 0)
                    self.assertLessEqual(len(payload), 8192 - 256)
                    self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)
                    self.assertEqual(h["payload_sha256"], hashlib.sha256(payload).digest())
                    self.assertTrue(header_crc_ok(raw))
                    self.assertEqual(raw[64:252], b"\xff" * 188)
                    sp, reset = struct.unpack_from("<II", payload, 0)
                    self.assertEqual(sp, RAM_HI)
                    lo = mk.SLOT_BASE[slot] + 0x100
                    self.assertTrue(reset & 1, "thumb bit")
                    self.assertTrue(lo <= (reset & ~1) < lo + len(payload), hex(reset))
                    self.assertEqual(payload, mk.pad8((BUILD / f"app_{var}_{slot}.bin").read_bytes()))

    def test_release_set(self):
        rel = IMAGES / "release"
        m = json.loads((rel / "manifest.json").read_text())
        top = json.loads((IMAGES / "manifest.json").read_text())
        self.assertEqual(set(m), {p.name for p in rel.iterdir() if p.name != "manifest.json"})
        self.assertEqual({n for n in top if n.endswith(".img")}, {n for n in m if n.endswith(".img")})
        for var, info in mk.APPS.items():
            for slot in mk.SLOTS:
                h, raw, payload = split((rel / f"{var}_{slot}.img").read_bytes())
                self.assertEqual(payload, mk.pad8((BUILD / f"app_{var}_{slot}_nohooks.bin").read_bytes()))
                self.assertTrue(header_crc_ok(raw))
                self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)
                self.assertNotIn(b"OK FAULT ARM ", payload, (var, slot))
                self.assertIn((info["version"] + info["suffix"]).encode() + b"\0", payload)
                self.assertFalse(m[f"{var}_{slot}.img"]["hooks"])
                self.assertTrue(top[f"{var}_{slot}.img"]["hooks"])
        for bad, code in mk.BAD.items():
            self.assertEqual(m[f"{bad}_A.img"]["expected_err"], code)

    def test_a_and_b_differ_only_by_link(self):
        a = split(img("v1_A.img"))[2]
        b = split(img("v1_B.img"))[2]
        self.assertEqual(len(a), len(b))
        self.assertNotEqual(a, b)

    def test_versions_distinct(self):
        hashes = {split(img(f"{v}_A.img"))[2] for v in mk.APPS}
        self.assertEqual(len(hashes), len(mk.APPS))

    def test_banner_strings(self):
        for var, m in mk.APPS.items():
            payload = split(img(f"{var}_A.img"))[2]
            self.assertIn((m["version"] + m["suffix"]).encode() + b"\0", payload, var)
            self.assertIn(b"BOOT app ", payload)
            self.assertIn(b" cfsr=0x00000000 hfsr=0x00000000", payload, "ARMv6-M: no CFSR/HFSR")
            if var != "v2hang":   # the hang variant never reaches the command loop
                for s in (b"PONG", b"VER " + m["version"].encode(), b"APP confirmed", b"OK UPD",
                          b"STATE active=", b"ERR unknown command", b"OK FAULT ARM ", b"OPTR 0x"):
                    self.assertIn(s, payload, (var, s))
                self.assertNotIn(b"ERR line too long", payload, "SPEC-ESP32 1: ERR unknown command")


class TestBadImages(unittest.TestCase):
    def test_bad_crc(self):
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"bad_crc_{slot}.img"))
            good = split(img(f"v2_{slot}.img"))
            self.assertEqual(raw, good[1], "same header as v2")
            self.assertTrue(header_crc_ok(raw))
            self.assertEqual(len(payload), len(good[2]))
            self.assertEqual(sum(x != y for x, y in zip(payload, good[2])), 1)
            self.assertNotEqual(zlib.crc32(payload) & 0xFFFFFFFF, h["payload_crc32"])

    def test_wrong_board_is_the_real_f407_v2(self):
        """SPEC-C031C6 2: a real F407 image (board 0xF407D15C) in the C0 header format."""
        f4 = (F407 / "build" / "images" / "v2_A.img")
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"wrong_board_{slot}.img"))
            f4bin = (F407 / "build" / f"app_v2_{slot}.bin").read_bytes()
            self.assertEqual(payload, mk.pad8(f4bin))
            self.assertEqual(h["board_id"], 0xF407D15C)
            self.assertEqual(h["link_addr"], {"A": 0x08008200, "B": 0x0800C200}[slot])
            self.assertEqual(h["fw_version"], 0x020000)
            self.assertEqual(h["payload_size"], len(payload))
            self.assertEqual(len(payload) % 8, 0)
            self.assertTrue(header_crc_ok(raw))
            self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)
            self.assertEqual(struct.unpack_from("<I", payload, 0)[0], 0x20020000, "F407 stack top")
        if f4.exists():   # same board id and link address as the F407's own v2 image
            f4h = mk.parse_header(f4.read_bytes()[:256])
            self.assertEqual(f4h["board_id"], 0xF407D15C)
            self.assertEqual(f4h["link_addr"], 0x08008200)

    def test_truncated(self):
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"truncated_{slot}.img"))
            good = split(img(f"v2_{slot}.img"))
            self.assertEqual(raw, good[1])
            self.assertEqual(len(payload), (h["payload_size"] // 2) & ~7)
            self.assertEqual(payload, good[2][:len(payload)])

    def test_oversize(self):
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"oversize_{slot}.img"))
            self.assertEqual(h["payload_size"], 9000)
            self.assertGreater(h["payload_size"], mk.PAYLOAD_MAX)
            self.assertEqual(len(payload), 9000)
            self.assertTrue(header_crc_ok(raw))
            self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)

    def test_old(self):
        for slot in mk.SLOTS:
            h = split(img(f"old_{slot}.img"))[0]
            self.assertEqual(h["security_counter"], 0)
            self.assertEqual(h["fw_version"], 0x000900)


def unpack_rec(rec: bytes):
    return struct.unpack("<IIBBBBIIIII", rec)


class TestGolden(unittest.TestCase):
    def check(self, name: str, bl: str, v1: bytes | None = None, d: Path = IMAGES):
        g = (d / name).read_bytes()
        self.assertEqual(len(g), GOLDEN)
        b = (BUILD / f"{bl}.bin").read_bytes()
        self.assertLessEqual(len(b), 12288)
        self.assertEqual(g[:len(b)], b)
        self.assertEqual(g[len(b):0x3000], b"\xff" * (0x3000 - len(b)))
        rec = g[0x3000:0x3020]
        (magic, seq, kind, active, trial, attempts, min_sc, e_self, e_other, res, crc) = unpack_rec(rec)
        self.assertEqual((magic, seq, kind, active, trial, attempts, min_sc, e_self, e_other, res),
                         (0x4154454D, 1, 1, 0, 0xFF, 0, 1, 0, 0, 0xFFFFFFFF))
        self.assertEqual(crc, zlib.crc32(rec[:28]) & 0xFFFFFFFF)
        self.assertEqual(g[0x3020:0x4000], b"\xff" * (0x4000 - 0x3020), "rest of META0 + META1 erased")
        v1 = v1 or img("v1_A.img")
        self.assertEqual(g[0x4000:0x4000 + len(v1)], v1)
        self.assertEqual(g[0x4000 + len(v1):0x6000], b"\xff" * (0x6000 - 0x4000 - len(v1)))
        self.assertEqual(g[0x6000:GOLDEN], b"\xff" * (GOLDEN - 0x6000), "B erased")

    def test_golden_ab(self):
        rel_v1 = (IMAGES / "release" / "v1_A.img").read_bytes()
        self.check("golden_ab.bin", "bl_ab_hooks")
        self.check("golden_ab_nohooks.bin", "bl_ab", rel_v1)
        self.check("golden_ab.bin", "bl_ab", rel_v1, IMAGES / "release")

    def test_golden_metafull(self):
        g, base = img("golden_ab_metafull.bin"), img("golden_ab.bin")
        self.assertEqual(len(g), GOLDEN)
        self.assertEqual(g[:0x3000], base[:0x3000], "same hooks BL")
        self.assertEqual(g[0x3800:], base[0x3800:], "same META1 (erased) and slots")
        n = mk.META_FULL_RECORDS
        self.assertEqual(n, 63)
        for i in range(n):
            rec = g[0x3000 + 32 * i:0x3000 + 32 * i + 32]
            (magic, seq, kind, active, trial, attempts, min_sc, e_self, e_other, res, crc) = unpack_rec(rec)
            self.assertEqual((magic, seq, active, trial, attempts, min_sc, e_self, e_other),
                             (0x4154454D, i + 1, 0, 0xFF, 0, 1, 0, 0))
            self.assertEqual(kind, 1 if i == 0 else 4)
            self.assertEqual(crc, zlib.crc32(rec[:28]) & 0xFFFFFFFF)
        self.assertEqual(g[0x3000 + 32 * n:0x3800], b"\xff" * 32, "exactly one free record slot")
        m = json.loads((IMAGES / "manifest.json").read_text())
        self.assertEqual(m["golden_ab_metafull.bin"]["meta0_records"], 63)
        self.assertEqual(m["golden_ab.bin"]["meta0_records"], 1)

    def test_golden_naive(self):
        rel_v1 = (IMAGES / "release" / "v1_A.img").read_bytes()
        self.check("golden_naive.bin", "bl_naive_hooks")
        self.check("golden_naive_nohooks.bin", "bl_naive", rel_v1)
        self.check("golden_naive.bin", "bl_naive", rel_v1, IMAGES / "release")

    def test_hex_matches_bin(self):
        for d, names in ((IMAGES, ("golden_ab", "golden_naive", "golden_ab_nohooks", "golden_naive_nohooks",
                                   "golden_ab_metafull")),
                         (IMAGES / "release", ("golden_ab", "golden_naive"))):
            for name in names:
                mem: dict[int, int] = {}
                upper = 0
                for line in (d / f"{name}.hex").read_text().split():
                    raw = bytes.fromhex(line[1:])
                    self.assertEqual(sum(raw) & 0xFF, 0, "checksum")
                    n, addr, typ, data = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
                    if typ == 4:
                        upper = int.from_bytes(data, "big") << 16
                    elif typ == 0:
                        for i, x in enumerate(data):
                            mem[upper + addr + i] = x
                self.assertEqual(min(mem), 0x08000000)
                self.assertEqual(max(mem), 0x08007FFF)
                self.assertEqual(len(mem), GOLDEN)
                self.assertEqual(bytes(mem[0x08000000 + i] for i in range(GOLDEN)),
                                 (d / f"{name}.bin").read_bytes())


class TestManifest(unittest.TestCase):
    def test_manifest(self):
        for d in (IMAGES, IMAGES / "release"):
            m = json.loads((d / "manifest.json").read_text())
            files = {p.name for p in d.iterdir() if p.is_file() and p.name != "manifest.json"}
            self.assertEqual(set(m), files)
            for name, info in m.items():
                with self.subTest(name=name):
                    data = (d / name).read_bytes()
                    self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(info["size"], len(data))
                    self.assertEqual(info["target"], "c031")
                    self.assertIn(info["slot"], ("A", "B"))
                    self.assertRegex(info["version"], r"^\d+\.\d+\.\d+$")
                    if name.endswith(".img"):
                        payload = data[256:]
                        self.assertEqual(info["payload_sha256"], hashlib.sha256(payload).hexdigest())
        m = json.loads((IMAGES / "manifest.json").read_text())
        for v in mk.APPS:
            for s in mk.SLOTS:
                self.assertIn(f"{v}_{s}.img", m)
        for bad, code in mk.BAD.items():
            self.assertEqual(m[f"{bad}_A.img"]["expected_err"], code)
        self.assertIsNone(m["v2_A.img"]["expected_err"])
        self.assertEqual(m["golden_ab.bin"]["payload_sha256"], m["v1_A.img"]["payload_sha256"])

    def test_same_keys_as_f407(self):
        """The C0 manifest has the F407 keys (plus target), so the host reads both the same way."""
        f4 = F407 / "build" / "images" / "manifest.json"
        if not f4.exists():
            self.skipTest("F407 build missing")
        a, b = json.loads(f4.read_text()), json.loads((IMAGES / "manifest.json").read_text())
        self.assertEqual(set(a), set(b))
        for name in a:
            self.assertLessEqual(set(a[name]), set(b[name]), name)


class TestSingleImageCli(unittest.TestCase):
    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.img"
            subprocess.run([sys.executable, str(BOOT / "mkimage.py"), "--payload",
                            str(BUILD / "app_v2_B.bin"), "--version", "2.0.0", "--sc", "1",
                            "--slot", "B", "-o", str(out)], check=True, capture_output=True)
            self.assertEqual(out.read_bytes(), img("v2_B.img"))


BRANCH = re.compile(r"^(bl|blx|bx|b|b\.n|b\.w|b[a-z]{2}(\.[nw])?)$")


@unittest.skipUnless(OBJDUMP and NM and READELF, "arm-none-eabi binutils not found")
class TestElfStatic(unittest.TestCase):
    BLS = ("bl_ab", "bl_ab_hooks", "bl_naive", "bl_naive_hooks")

    def syms(self, elf: str) -> dict[str, int]:
        out = subprocess.run([NM, str(BUILD / f"{elf}.elf")], capture_output=True, text=True).stdout
        return {p[2]: int(p[0], 16) for p in (l.split() for l in out.splitlines()) if len(p) == 3}

    def elves(self):
        return list(self.BLS) + [f"app_{v}_{s}{h}" for v in mk.APPS for s in mk.SLOTS for h in ("", "_nohooks")]

    @staticmethod
    def hooks(e: str) -> bool:
        return e.endswith("_hooks") or (e.startswith("app") and not e.endswith("_nohooks"))

    def test_sizes(self):
        for bl in self.BLS:
            self.assertLessEqual((BUILD / f"{bl}.bin").stat().st_size, 12288, bl)
        for e in self.elves():
            if e.startswith("app"):
                self.assertLessEqual((BUILD / f"{e}.bin").stat().st_size, 8192 - 256, e)

    def test_armv6m(self):
        for e in self.elves():
            out = subprocess.run([READELF, "-A", str(BUILD / f"{e}.elf")], capture_output=True, text=True).stdout
            self.assertRegex(out, r"Tag_CPU_arch: v6S?-M", e)
            self.assertNotIn("Tag_FP_arch", out, e)

    def test_noinit_and_ramvec(self):
        for e in self.elves():
            s = self.syms(e)
            self.assertEqual(s["g_noinit"], 0x20000000, e)
            self.assertEqual(s["g_ram_vectors"] % 256, 0, e)
            self.assertGreaterEqual(s["g_ram_vectors"], 0x20000100, e)
            self.assertLess(s["_ebss"] + 0x800, RAM_HI, e)

    def test_vectors(self):
        for e in self.elves():
            with self.subTest(elf=e):
                s = self.syms(e)
                b = (BUILD / f"{e}.bin").read_bytes()
                vec = struct.unpack_from("<48I", b, 0)
                base = 0x08000000 if e.startswith("bl") else mk.SLOT_BASE[e.split("_")[2]] + 0x100
                self.assertEqual(s["g_vectors"], base)
                self.assertEqual(base % 256, 0, "VTOR alignment")
                self.assertEqual(vec[0], RAM_HI)
                tim3, tim14 = vec[16 + 16], vec[16 + 19]
                if self.hooks(e):   # TIM3 (IRQ 16) -> RAM ISR
                    self.assertEqual(tim3, s["TIM3_IRQHandler"] | 1)
                    self.assertTrue(RAM_LO <= tim3 < RAM_HI)
                else:   # release: TIM3 is the weak default handler
                    self.assertEqual(tim3, s["Default_Handler"] | 1)
                self.assertEqual(tim14, s["TIM14_IRQHandler"] | 1)
                self.assertTrue(RAM_LO <= tim14 < RAM_HI, "time base ISR in RAM")
                if e.startswith("app"):   # SysTick -> RAM
                    self.assertTrue(RAM_LO <= vec[15] < RAM_HI)
                self.assertTrue(vec[3] & 1 and 0x08000000 <= vec[3] < 0x08008000, "HardFault in flash")

    def test_ram_placement(self):
        """SPEC 5: the flash routines and the ISRs that can run during a flash operation are in RAM."""
        for e in self.elves():
            s = self.syms(e)
            names = ["ram_erase", "ram_program", "ram_icache_reset", "TIM14_IRQHandler"]
            if self.hooks(e):
                names.append("TIM3_IRQHandler")
            if e.startswith("app"):
                names.append("SysTick_Handler")
            for fn in names:
                self.assertTrue(RAM_LO <= s[fn] < RAM_HI, (e, fn, hex(s.get(fn, 0))))

    def test_ramfunc_never_touches_flash(self):
        """SPEC 5: code that runs while the flash is busy must not fetch from flash."""
        for e in self.elves():
            with self.subTest(elf=e):
                out = subprocess.run([OBJDUMP, "-d", "-j", ".ramfunc", str(BUILD / f"{e}.elf")],
                                     capture_output=True, text=True).stdout
                self.assertIn("<ram_erase>:", out)
                self.assertIn("<ram_program>:", out)
                for line in out.splitlines():
                    m = re.match(r"\s*[0-9a-f]+:\s+(?:[0-9a-f]{4}\s?){1,2}\s+(\S+)\s*(.*)", line)
                    if not m:
                        continue
                    op, args = m.groups()
                    self.assertFalse(BRANCH.match(op) and re.search(r"\b80[0-9a-f]{5}\b", args), line)
                    self.assertFalse(op.startswith("udf"), line)
                    if op == ".word":
                        val = int(args.split()[0], 16)
                        self.assertFalse(0x08000000 <= val < 0x08100000, line)

    def test_flash_driver_guard(self):
        """Pages 0-5 (BL) and anything outside 6..15 are refused; no mass erase, no option bytes."""
        raw = (BOOT / "flash.c").read_text()
        src = strip_comments(raw)
        for word in ("MER", "OPTKEYR", "OPTSTRT", "OBL_LAUNCH", "FLASH_OPTR", "0x08192A3B", "0x4C5D6E7F"):
            self.assertNotIn(word, src, word)
        self.assertIn("first < PAGE_MIN_ERASABLE || first > PAGE_MAX_ERASABLE", src)
        self.assertIn("n > PAGE_MAX_ERASABLE + 1u - first", src)
        lay = (BOOT / "layout.h").read_text()
        self.assertRegex(lay, r"PAGE_MIN_ERASABLE\s+6u")
        self.assertRegex(lay, r"PAGE_MAX_ERASABLE\s+15u")
        self.assertIn("CR_PNB(n)    ((n) << 3)", src)
        # the CR bits the driver writes: PG, PER, PNB, STRT, LOCK; nothing else (MER1 is bit 2)
        for m in re.finditer(r"#define CR_\w+\s+\(1u << (\d+)\)", src):
            self.assertIn(int(m.group(1)), (0, 1, 16, 31), m.group(0))
        # Option bytes are only read (app OPTR command); nothing ever writes FLASH_OPTR / OPTKEYR
        for f in BOOT.glob("*.c"):
            self.assertNotRegex(strip_comments(f.read_text()), r"FLASH_OPTR\s*=[^=]", f.name)
            self.assertNotIn("OPTKEYR", f.read_text(), f.name)
        # every erase goes through the guarded entry point
        for f in BOOT.glob("*.c"):
            if f.name != "flash.c":
                self.assertNotIn("ram_erase", f.read_text(), f.name)

    def test_page_guard_values(self):
        """Model of flash_erase_pages()'s guard: exactly pages 6..15 are reachable."""
        def ok(first, n):
            return not (n == 0 or first < 6 or first > 15 or n > 15 + 1 - first)
        self.assertTrue(ok(6, 1) and ok(7, 1) and ok(8, 4) and ok(12, 4) and ok(15, 1) and ok(6, 10))
        for first, n in ((0, 1), (5, 1), (5, 2), (16, 1), (12, 5), (15, 2), (6, 11), (8, 0),
                         (6, 0xFFFFFFFF), (0xFFFFFFFF, 1)):
            self.assertFalse(ok(first, n), (first, n))

    def test_status_line_strings(self):
        for bl, design, hooks in (("bl_ab", "ab", ""), ("bl_ab_hooks", "ab", " hooks"),
                                  ("bl_naive", "naive", ""), ("bl_naive_hooks", "naive", " hooks")):
            b = (BUILD / f"{bl}.bin").read_bytes()
            self.assertIn(f"BL v0.1 design={design}{hooks} target=c031 slotA=".encode(), b, bl)
            self.assertNotIn(b"target=f407", b, bl)
            self.assertNotIn(b"ERR unknown command", b, bl)
            if design == "ab":
                self.assertIn(b"BL resume", b, bl)
            else:
                self.assertNotIn(b"BL resume", b, (bl, "naive must not resume (SPEC 4)"))
            for s in (b"BL nofw", b"BL update", b"BL timeout", b"OK COMMIT", b"OK ABORT", b"PONG",
                      b"OK ERASED ", b"ERR BAD_HDR", b"ERR LINE", b"ERR STATE", b"ERR SIZE", b"ERR FLASH",
                      b" cfsr=0x00000000 hfsr=0x00000000"):
                self.assertIn(s, b, (bl, s))
            if design == "ab":
                for s in (b"BL rollback ", b"BL repair active=", b"ERR IMG_CRC", b"ERR IMG_SHA",
                          b"ERR VERSION", b"ERR BOARD", b"ERR LINK", b"ERR SHORT"):
                    self.assertIn(s, b, (bl, s))
            else:
                self.assertIn(b" slotB=- active=", b)
                self.assertIn(b" state=none attempts=0 seq=0 erases=0,0", b)
                self.assertNotIn(b"ERR IMG_CRC", b, "naive has no image check")
            self.assertEqual(b"BL fault_fired " in b, bool(hooks), bl)
            self.assertEqual(b"BL fault_cancelled " in b, bool(hooks), bl)
            self.assertEqual(b"OK FAULT ARM " in b, bool(hooks), bl)


def c_func(src: str, signature: str) -> str:
    """Body of the C function that starts with `signature` (up to the matching brace)."""
    i = src.index(signature)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[j:k + 1]
    raise AssertionError(signature)


def in_order(test, text: str, *parts: str):
    """Each part occurs, in this order (each searched after the previous one)."""
    pos = 0
    for x in parts:
        i = text.find(x, pos)
        test.assertGreaterEqual(i, 0, (x, "missing or out of order", parts))
        pos = i + len(x)


class TestSourceContract(unittest.TestCase):
    """C behaviour that the host simulator mirrors (SPEC 10), with the C0 overrides."""

    def src(self, name: str) -> str:
        return strip_comments((BOOT / name).read_text())

    def test_data_check_order(self):
        body = c_func(self.src("bl_update.c"), "static void cmd_data(")
        in_order(self, body, "err_line()", 'uart_line("ERR STATE")', 'reply_err_off("CRC", off)',
                 'reply_err_off("SEQ", x.written)', 'uart_line("ERR SIZE")', "fault_point(PH_DATA")
        self.assertEqual(body.count("err_line()"), 2, "malformed DATA -> ERR LINE")
        # SPEC-C031C6 4: off and length multiples of 8, else ERR SEQ
        self.assertIn("(off & (PROG_UNIT - 1u)) || (len & (PROG_UNIT - 1u)) || off > x.written", body)

    def test_line_limit_400_for_every_line(self):
        self.assertRegex((BOOT / "bl.h").read_text(), r"#define UPD_LINE_MAX 400u")
        upd = self.src("bl_update.c")
        self.assertNotIn("BEGIN_MAX", upd)
        self.assertNotIn("700", upd)
        main = c_func(self.src("bl.c"), "int main(")
        self.assertIn("line_init(s_line_u1, s_line_u2, UPD_LINE_MAX);", main)

    def test_update_mode_unknown_line_and_abort(self):
        body = c_func(self.src("bl_update.c"), "void bl_update_mode(")
        self.assertNotIn("unknown command", body)
        tail = body[body.rindex("} else {"):]
        self.assertIn("err_line();", tail)
        abort = body[body.index('str_eq(l, "UPD ABORT")'):body.index('str_eq(l, "PING")')]
        self.assertIn("return;", abort)
        self.assertNotIn("system_reset", abort, "ABORT boots on, it does not reset (SPEC 4)")
        # SPEC-ESP32 1: the inactivity timer restarts on a valid command, not on garbage
        self.assertIn("if (!s_err_line) t_last = time_us();", body)
        in_order(self, body, "if (n < 0)", "err_line();", "continue;")

    def test_two_uarts(self):
        hw = self.src("hw.c")
        self.assertIn("static const uint32_t UBASE[NUM_UARTS] = {USART1_BASE, USART2_BASE};", hw)
        putc = c_func(hw, "void uart_putc(char c)")
        self.assertIn("for (uint32_t u = 0; u < NUM_UARTS; u++)", putc)
        self.assertIn("U_TDR(b)", putc)
        poll = c_func(hw, "int line_poll(uint32_t *u_out)")
        self.assertIn("s_line[u]", poll)
        self.assertIn("if (c == '\\0') s_line[u].bad = 1;", poll)
        init = c_func(hw, "void hw_init(void)")
        self.assertIn("pu |= (1u << 6) | (1u << 20);", init, "PA3 + PA10 pull-ups")
        self.assertNotRegex(init, r"\b(2[6-9]|30)u?\b", "PA13/PA14 (bits 26-29 of MODER) untouched")
        app = c_func(self.src("app_rt.c"), "void rt_run(int may_confirm)")
        self.assertIn('uart_line("ERR unknown command")', app)

    def test_clock_latency_before_48mhz(self):
        clk = c_func(self.src("hw.c"), "static void clock_init(void)")
        in_order(self, clk, "FLASH_ACR = (acr & ~ACR_LATENCY_MSK) | 1u;", "RCC_CR &= ~RCC_CR_HSIDIV_MSK;")
        c = (BOOT / "common.h").read_text()
        self.assertIn("#define SYSCLK_HZ    48000000u", c)
        self.assertIn("#define UID_BASE     0x1FFF7550u", c)

    def test_flash_sequence(self):
        f = self.src("flash.c")
        prog = c_func(f, "RAMFUNC static uint32_t ram_program(")
        in_order(self, prog, "while (FLASH_SR & SR_BUSY)", "FLASH_SR = SR_CLEAR;", "FLASH_CR = CR_PG;",
                 'cpsid i', "dst[2u * i] = src[2u * i];", "dst[2u * i + 1u] = src[2u * i + 1u];",
                 "msr primask", "while (FLASH_SR & SR_BUSY)", "err = FLASH_SR & SR_ERRORS;",
                 "FLASH_CR = 0;", "ram_icache_reset();")
        er = c_func(f, "RAMFUNC static uint32_t ram_erase(")
        in_order(self, er, "while (FLASH_SR & SR_BUSY)", "FLASH_SR = SR_CLEAR;", "CR_PER | CR_PNB(p)",
                 "CR_STRT", "while (FLASH_SR & SR_BUSY)", "ram_icache_reset();")
        self.assertIn("#define SR_BUSY      (SR_BSY1 | SR_CFGBSY)", f)
        ic = c_func(f, "RAMFUNC static void ram_icache_reset(")
        in_order(self, ic, "acr & ~ACR_ICEN", "ACR_ICRST", "acr & ~ACR_ICRST")
        fp = c_func(f, "int flash_program(")
        self.assertIn("if (!flash_is_erased(addr, len)) return -3;", fp, "never re-program a double-word")

    def test_fault_counted_at_expiry(self):
        src = self.src("fault.c")
        isr = c_func(src, "RAMISR void TIM3_IRQHandler(")
        in_order(self, isr, "wraps_left--", "TIM_SR_CC1IF", "record_expiry();", "SCB_AIRCR = AIRCR_SYSRESETREQ")
        fp = c_func(src, "uint32_t fault_point(")
        self.assertNotIn("expiry.count", fp)
        self.assertNotIn("fired", fp)
        in_order(self, fp, "noinit_commit();", "if (delay == 0)", "record_expiry();", "timer_start(delay);")
        rec = c_func(src, "static inline __attribute__((always_inline)) void record_expiry(")
        self.assertNotRegex(rec, r"\b(crc32|noinit_commit|uart_\w+)\s*\(", "the ISR must not call flash code")
        self.assertNotRegex(isr, r"\b(crc32|noinit_commit|uart_\w+|time_us)\s*\(")

    def test_timer_math(self):
        """Model of timer_start(): the compare at 0x8000 after wraps_left overflows is `ticks` away."""
        def plan(us):
            ticks = us if us > 1 else 2
            start = (0x8000 - (ticks & 0xFFFF)) & 0xFFFF
            return ticks, start, (start + ticks) >> 16

        def simulate(start, wraps):
            # compare matches happen at ticks k >= 1 with (start + k) % 65536 == 0x8000; the ISR
            # fires at the first one that comes after `wraps` overflows ((start + k) >> 16 of them)
            k = (0x8000 - start) % 0x10000 or 0x10000
            while (start + k) >> 16 < wraps:
                k += 0x10000
            return k

        for us in (1, 2, 3, 100, 0x7FFF, 0x8000, 0x8001, 0xFFFF, 0x10000, 0x10001, 0x18000,
                   123456, 2_000_000, 0xFFFFF, 10_000_000):
            ticks, start, wraps = plan(us)
            self.assertEqual(simulate(start, wraps), ticks, us)
            if wraps == 0:
                self.assertLess(start, 0x8000, us)
        body = c_func(self.src("fault.c"), "static void timer_start(")
        self.assertIn("uint32_t ticks = us > 1u ? us : 2u;", body)
        self.assertIn("uint32_t start = (FT_CMP - (ticks & 0xFFFFu)) & 0xFFFFu;", body)
        self.assertIn("g_noinit.ftimer.wraps_left = (start + ticks) >> 16;", body)
        in_order(self, body, "TIM_EGR(FT) = 1u;", "TIM_CNT(FT) = start;", "TIM_SR(FT) = 0;",
                 "TIM_CR1(FT) = TIM_CR1_URS | TIM_CR1_CEN;")

    def test_boot_snapshot_before_boot_fault_point(self):
        main = c_func(self.src("bl.c"), "int main(")
        in_order(self, main, "hw_init();", "noinit_check();", "fault_boot_snapshot();", "fault_point(PH_BOOT, 0)",
                 "RCC_CSR2 |= CSR2_RMVF;", "print_status();", "fault_print_report();", "boot_decision();")
        snap = c_func(self.src("fault.c"), "void fault_boot_snapshot(")
        self.assertNotIn("count -=", snap, "the snapshot clears nothing")

    def test_expiry_and_ftimer_outside_crc(self):
        h = (BOOT / "noinit.h").read_text()
        struct_ = h[h.index("typedef struct {\n    uint32_t magic;\n    uint32_t enter_update;"):]
        in_order(self, struct_, "uint32_t crc32;", "noinit_expiry_t expiry;", "noinit_ftimer_t ftimer;",
                 "} noinit_t;")
        chk = c_func(self.src("noinit.c"), "int noinit_check(")
        self.assertIn("offsetof(noinit_t, expiry)", chk)

    def test_jump_keeps_pending_fault_timer(self):
        j = c_func(self.src("bl.c"), "__attribute__((noreturn)) static void jump_to(")
        in_order(self, j, "fault_point(PH_JUMP", 'cpsid i', "if (!fault_timer_pending())",
                 "timebase_stop();", "SCB_VTOR = base;", "msr msp")

    def test_no_unaligned_pointer_casts(self):
        """ARMv6-M faults on an unaligned word load. Every cast to a pointer to a 32-bit (or wider)
        type must be one of these reviewed, aligned ones; a new cast must be reviewed and added."""
        allowed = {
            ("bl_update.c", "(const img_hdr_t *)hdr"),          # hdr = x.hdr (uint32_t[])
            ("bl_update.c", "(const img_hdr_t *)x.hdr"),
            ("meta.c", "(const meta_rec_t *)SEC_BASE"),          # page aligned
            ("common.h", "(const img_hdr_t *)SLOT_BASE"),        # 8 KB aligned
            ("bl.c", "(const volatile uint32_t *)base"),         # payload address, 256 aligned
            ("bl.c", "(const volatile uint32_t *)PAYLOAD_ADDR"),
            ("flash.c", "(volatile uint32_t *)addr"),            # checked: addr % 8 == 0
            ("flash.c", "(const volatile uint32_t *)addr"),      # callers pass aligned addresses
        }
        pat = re.compile(r"\(\s*(?:const\s+)?(?:volatile\s+)?(?:uint32_t|int32_t|uint64_t|uint16_t|"
                         r"img_hdr_t|meta_rec_t|noinit_\w+|sha256_ctx_t)\s*\*\s*\)\s*\(?\s*([A-Za-z_][\w.]*)")
        found = set()
        for f in sorted(list(BOOT.glob("*.c")) + list(BOOT.glob("*.h"))):
            for line in strip_comments(f.read_text()).splitlines():
                if "#define REG" in line:
                    continue
                for m in pat.finditer(line):
                    cast = re.sub(r"\s+", " ", m.group(0).split(")")[0] + ")") + m.group(1)
                    cast = cast.replace("( ", "(").replace(" )", ")")
                    found.add((f.name, cast))
        self.assertLessEqual(found, allowed, sorted(found - allowed))
        img_c = self.src("image.c")
        self.assertIn("rd32le(hdr + HDR_CRC_OFF)", img_c, "byte access for the header CRC")


if __name__ == "__main__":
    unittest.main()
