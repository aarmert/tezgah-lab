"""
Host-side checks for firmware/boot (no hardware):

    python -m unittest discover -s firmware/boot/tests -v

Builds firmware/boot/build/ first if it is missing (needs arm-none-eabi-gcc), then checks image
headers, CRCs, link addresses, the bad images, the golden layout, the manifest, and some static
properties of the ELFs (RAM functions really live in RAM and never call/branch into flash,
vector table entries, noinit placement, status line strings).
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
BUILD = BOOT / "build"
IMAGES = BUILD / "images"
sys.path.insert(0, str(BOOT))
import mkimage as mk  # noqa: E402

OBJDUMP = shutil.which("arm-none-eabi-objdump")
NM = shutil.which("arm-none-eabi-nm")


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
    return zlib.crc32(raw[:508]) & 0xFFFFFFFF == struct.unpack_from("<I", raw, 508)[0]


class TestLayout(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(mk.BL_BASE, 0x08000000)
        self.assertEqual(mk.META0_BASE, 0x08004000)
        self.assertEqual(mk.SLOT_BASE, {"A": 0x08008000, "B": 0x0800C000})
        self.assertEqual(mk.META1_BASE, 0x08010000)
        self.assertEqual(mk.SLOT_SIZE, 16384)
        self.assertEqual(mk.HDR_SIZE, 512)
        self.assertEqual(mk.PAYLOAD_MAX, 16384 - 512)
        self.assertEqual(mk.IMG_MAGIC, 0x4D495A54)
        self.assertEqual(mk.BOARD_ID, 0xF407D15C)
        self.assertEqual(mk.META_MAGIC, 0x4154454D)
        self.assertEqual(struct.pack("<I", mk.IMG_MAGIC), b"TZIM")
        self.assertEqual(struct.pack("<I", mk.META_MAGIC), b"META")

    def test_link_script_reserves_noinit(self):
        ld = (BOOT / "link.ld.in").read_text()
        self.assertRegex(ld, r"NOINIT\s*\(rwx\)\s*:\s*ORIGIN = 0x20000000, LENGTH = 256")
        self.assertRegex(ld, r"RAM\s*\(rwx\)\s*:\s*ORIGIN = 0x20000100")
        noinit_h = (BOOT / "noinit.h").read_text()
        self.assertIn("0x4E4F494E", noinit_h)


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
                    self.assertEqual(h["board_id"], mk.BOARD_ID)
                    self.assertEqual(h["fw_version"], mk.version_word(m["version"]))
                    self.assertEqual(h["security_counter"], m["sc"])
                    self.assertEqual(h["link_addr"], mk.SLOT_BASE[slot] + 0x200)
                    self.assertEqual(h["payload_size"], len(payload))
                    self.assertEqual(len(payload) % 4, 0)
                    self.assertLessEqual(len(payload), mk.PAYLOAD_MAX)
                    self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)
                    self.assertEqual(h["payload_sha256"], hashlib.sha256(payload).digest())
                    self.assertTrue(header_crc_ok(raw))
                    self.assertEqual(raw[64:508], b"\xff" * 444)
                    # payload is a Cortex-M image linked for this slot
                    sp, reset = struct.unpack_from("<II", payload, 0)
                    self.assertEqual(sp, 0x20020000)
                    lo = mk.SLOT_BASE[slot] + 0x200
                    self.assertTrue(reset & 1, "thumb bit")
                    self.assertTrue(lo <= (reset & ~1) < lo + len(payload), hex(reset))
                    # the payload equals the linked binary
                    self.assertEqual(payload, mk.pad4((BUILD / f"app_{var}_{slot}.bin").read_bytes()))

    def test_release_set(self):
        """images/release: the same names, built from the apps without FAULT_HOOKS (SPEC 2, 6)."""
        rel = IMAGES / "release"
        m = json.loads((rel / "manifest.json").read_text())
        top = json.loads((IMAGES / "manifest.json").read_text())
        self.assertEqual(set(m), {p.name for p in rel.iterdir() if p.name != "manifest.json"})
        self.assertEqual({n for n in top if n.endswith(".img")}, {n for n in m if n.endswith(".img")})
        for var, info in mk.APPS.items():
            for slot in mk.SLOTS:
                h, raw, payload = split((rel / f"{var}_{slot}.img").read_bytes())
                self.assertEqual(payload, mk.pad4((BUILD / f"app_{var}_{slot}_nohooks.bin").read_bytes()))
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
            if var != "v2hang":   # the hang variant never reaches the command loop
                for s in (b"PONG", b"VER " + m["version"].encode(), b"APP confirmed", b"OK UPD",
                          b"STATE active=", b"ERR unknown command", b"OK FAULT ARM "):
                    self.assertIn(s, payload, (var, s))


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

    def test_wrong_board(self):
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"wrong_board_{slot}.img"))
            self.assertEqual(h["board_id"], 0xC0C0C0C0)
            self.assertTrue(header_crc_ok(raw))
            self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)

    def test_truncated(self):
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"truncated_{slot}.img"))
            good = split(img(f"v2_{slot}.img"))
            self.assertEqual(raw, good[1])
            self.assertEqual(len(payload), (h["payload_size"] // 2) & ~3)
            self.assertEqual(payload, good[2][:len(payload)])

    def test_oversize(self):
        for slot in mk.SLOTS:
            h, raw, payload = split(img(f"oversize_{slot}.img"))
            self.assertEqual(h["payload_size"], 20000)
            self.assertGreater(h["payload_size"], mk.PAYLOAD_MAX)
            self.assertEqual(len(payload), 20000)
            self.assertTrue(header_crc_ok(raw))
            self.assertEqual(h["payload_crc32"], zlib.crc32(payload) & 0xFFFFFFFF)

    def test_old(self):
        for slot in mk.SLOTS:
            h = split(img(f"old_{slot}.img"))[0]
            self.assertEqual(h["security_counter"], 0)
            self.assertEqual(h["fw_version"], 0x000900)


class TestGolden(unittest.TestCase):
    def check(self, name: str, bl: str, v1: bytes | None = None, d: Path = IMAGES):
        g = (d / name).read_bytes()
        self.assertEqual(len(g), 0x20000)
        b = (BUILD / f"{bl}.bin").read_bytes()
        self.assertLessEqual(len(b), 16384)
        self.assertEqual(g[:len(b)], b)
        self.assertEqual(g[len(b):0x4000], b"\xff" * (0x4000 - len(b)))
        rec = g[0x4000:0x4020]
        (magic, seq, kind, active, trial, attempts, min_sc, e_self, e_other, res, crc) = \
            struct.unpack("<IIBBBBIIIII", rec)
        self.assertEqual((magic, seq, kind, active, trial, attempts, min_sc, e_self, e_other, res),
                         (0x4154454D, 1, 1, 0, 0xFF, 0, 1, 0, 0, 0xFFFFFFFF))
        self.assertEqual(crc, zlib.crc32(rec[:28]) & 0xFFFFFFFF)
        self.assertEqual(g[0x4020:0x8000], b"\xff" * (0x8000 - 0x4020))
        v1 = v1 or img("v1_A.img")
        self.assertEqual(g[0x8000:0x8000 + len(v1)], v1)
        self.assertEqual(g[0x8000 + len(v1):0xC000], b"\xff" * (0xC000 - 0x8000 - len(v1)))
        self.assertEqual(g[0xC000:0x20000], b"\xff" * (0x20000 - 0xC000), "B and META1 erased")

    def test_golden_ab(self):
        rel_v1 = (IMAGES / "release" / "v1_A.img").read_bytes()
        self.check("golden_ab.bin", "bl_ab_hooks")
        self.check("golden_ab_nohooks.bin", "bl_ab", rel_v1)          # release BL + release app
        self.check("golden_ab.bin", "bl_ab", rel_v1, IMAGES / "release")

    def test_golden_metafull(self):
        g, base = img("golden_ab_metafull.bin"), img("golden_ab.bin")
        self.assertEqual(len(g), 0x20000)
        self.assertEqual(g[:0x4000], base[:0x4000], "same hooks BL")
        self.assertEqual(g[0x8000:], base[0x8000:], "same slots and META1 (erased)")
        n = mk.META_FULL_RECORDS
        self.assertEqual(n, 511)
        for i in range(n):
            rec = g[0x4000 + 32 * i:0x4000 + 32 * i + 32]
            (magic, seq, kind, active, trial, attempts, min_sc, e_self, e_other, res, crc) = \
                struct.unpack("<IIBBBBIIIII", rec)
            self.assertEqual((magic, seq, active, trial, attempts, min_sc, e_self, e_other),
                             (0x4154454D, i + 1, 0, 0xFF, 0, 1, 0, 0))
            self.assertEqual(kind, 1 if i == 0 else 4)
            self.assertEqual(crc, zlib.crc32(rec[:28]) & 0xFFFFFFFF)
        self.assertEqual(g[0x4000 + 32 * n:0x8000], b"\xff" * 32, "exactly one free record slot")
        m = json.loads((IMAGES / "manifest.json").read_text())
        self.assertEqual(m["golden_ab_metafull.bin"]["meta0_records"], 511)
        self.assertEqual(m["golden_ab.bin"]["meta0_records"], 1)

    def test_golden_naive(self):
        rel_v1 = (IMAGES / "release" / "v1_A.img").read_bytes()
        self.check("golden_naive.bin", "bl_naive_hooks")
        self.check("golden_naive_nohooks.bin", "bl_naive", rel_v1)
        self.check("golden_naive.bin", "bl_naive", rel_v1, IMAGES / "release")

    def test_hex_matches_bin(self):
        for name in ("golden_ab", "golden_naive", "golden_ab_nohooks", "golden_naive_nohooks", "golden_ab_metafull"):
            mem: dict[int, int] = {}
            upper = 0
            for line in (IMAGES / f"{name}.hex").read_text().split():
                raw = bytes.fromhex(line[1:])
                self.assertEqual(sum(raw) & 0xFF, 0, "checksum")
                n, addr, typ, data = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
                if typ == 4:
                    upper = int.from_bytes(data, "big") << 16
                elif typ == 0:
                    for i, x in enumerate(data):
                        mem[upper + addr + i] = x
            self.assertEqual(min(mem), 0x08000000)
            self.assertEqual(len(mem), 0x20000)
            self.assertEqual(bytes(mem[0x08000000 + i] for i in range(0x20000)), img(f"{name}.bin"))


class TestManifest(unittest.TestCase):
    def test_manifest(self):
        m = json.loads((IMAGES / "manifest.json").read_text())
        files = {p.name for p in IMAGES.iterdir() if p.is_file() and p.name != "manifest.json"}
        self.assertEqual(set(m), files)
        for name, info in m.items():
            with self.subTest(name=name):
                data = img(name)
                self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(info["size"], len(data))
                self.assertIn(info["slot"], ("A", "B"))
                self.assertRegex(info["version"], r"^\d+\.\d+\.\d+$")
                if name.endswith(".img"):
                    payload = data[512:]
                    self.assertEqual(info["payload_sha256"], hashlib.sha256(payload).hexdigest())
        for v in mk.APPS:
            for s in mk.SLOTS:
                self.assertIn(f"{v}_{s}.img", m)
        for bad, code in mk.BAD.items():
            self.assertEqual(m[f"{bad}_A.img"]["expected_err"], code)
        self.assertIsNone(m["v2_A.img"]["expected_err"])
        self.assertEqual(m["golden_ab.bin"]["payload_sha256"], m["v1_A.img"]["payload_sha256"])


class TestSingleImageCli(unittest.TestCase):
    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.img"
            subprocess.run([sys.executable, str(BOOT / "mkimage.py"), "--payload",
                            str(BUILD / "app_v2_B.bin"), "--version", "2.0.0", "--sc", "1",
                            "--slot", "B", "-o", str(out)], check=True, capture_output=True)
            self.assertEqual(out.read_bytes(), img("v2_B.img"))


@unittest.skipUnless(OBJDUMP and NM, "arm-none-eabi binutils not found")
class TestElfStatic(unittest.TestCase):
    BLS = ("bl_ab", "bl_ab_hooks", "bl_naive", "bl_naive_hooks")

    def syms(self, elf: str) -> dict[str, int]:
        out = subprocess.run([NM, str(BUILD / f"{elf}.elf")], capture_output=True, text=True).stdout
        return {p[2]: int(p[0], 16) for p in (l.split() for l in out.splitlines()) if len(p) == 3}

    def elves(self):
        return list(self.BLS) + [f"app_{v}_{s}{h}" for v in mk.APPS for s in mk.SLOTS for h in ("", "_nohooks")]

    def test_bl_sizes(self):
        for bl in self.BLS:
            self.assertLessEqual((BUILD / f"{bl}.bin").stat().st_size, 16384)

    def test_noinit_and_ramvec(self):
        for e in self.elves():
            s = self.syms(e)
            self.assertEqual(s["g_noinit"], 0x20000000, e)
            self.assertEqual(s["g_ram_vectors"] % 512, 0, e)
            self.assertGreaterEqual(s["g_ram_vectors"], 0x20000100, e)

    def test_vectors(self):
        for e in self.elves():
            with self.subTest(elf=e):
                s = self.syms(e)
                b = (BUILD / f"{e}.bin").read_bytes()
                vec = struct.unpack_from("<98I", b, 0)
                base = 0x08000000 if e.startswith("bl") else mk.SLOT_BASE[e.split("_")[2]] + 0x200
                self.assertEqual(s["g_vectors"], base)
                self.assertEqual(vec[0], 0x20020000)
                hooks = e.endswith("_hooks") or (e.startswith("app") and not e.endswith("_nohooks"))
                if hooks:   # TIM2 (IRQ 28) -> RAM ISR
                    self.assertEqual(vec[16 + 28], s["TIM2_IRQHandler"] | 1)
                    self.assertTrue(0x20000000 <= vec[16 + 28] < 0x20020000)
                else:   # release build: TIM2 is just the weak default handler
                    self.assertEqual(vec[16 + 28], s["Default_Handler"] | 1)
                if e.startswith("app"):   # SysTick -> RAM
                    self.assertTrue(0x20000000 <= vec[15] < 0x20020000)

    def test_ramfunc_never_touches_flash(self):
        """SPEC 5: code that runs while the flash is busy must not fetch from flash."""
        for e in self.elves():
            with self.subTest(elf=e):
                s = self.syms(e)
                for fn in ("ram_erase", "ram_program"):
                    self.assertTrue(0x20000000 <= s[fn] < 0x20020000, (e, fn))
                out = subprocess.run([OBJDUMP, "-d", "-j", ".ramfunc", str(BUILD / f"{e}.elf")],
                                     capture_output=True, text=True).stdout
                self.assertIn("<ram_erase>:", out)
                for line in out.splitlines():
                    m = re.match(r"\s*[0-9a-f]+:\s+(?:[0-9a-f]{4}\s?){1,2}\s+(\S+)\s*(.*)", line)
                    if not m:
                        continue
                    op, args = m.groups()
                    self.assertFalse(re.match(r"^(bl|blx|b|b\.w|b\.n|b[a-z]{2}(\.[nw])?)$", op)
                                     and re.search(r"\b8[0-9a-f]{6}\b", args), line)
                    if op == ".word":
                        val = int(args.split()[0], 16)
                        self.assertFalse(0x08000000 <= val < 0x08100000, line)

    def test_flash_guard_constants(self):
        """flash.c refuses sector 0 and programming below META0."""
        src = (BOOT / "flash.c").read_text()
        self.assertIn("sector < SECTOR_MIN_ERASABLE || sector > SECTOR_MAX_ERASABLE", src)
        self.assertNotIn("MER", re.sub(r"/\*.*?\*/", "", src, flags=re.S))
        self.assertNotIn("OPTKEYR", src)
        lay = (BOOT / "layout.h").read_text()
        self.assertRegex(lay, r"SECTOR_MIN_ERASABLE\s+1u")
        self.assertRegex(lay, r"SECTOR_MAX_ERASABLE\s+4u")

    def test_status_line_strings(self):
        for bl, design, hooks in (("bl_ab", "ab", ""), ("bl_ab_hooks", "ab", " hooks"),
                                  ("bl_naive", "naive", ""), ("bl_naive_hooks", "naive", " hooks")):
            b = (BUILD / f"{bl}.bin").read_bytes()
            self.assertIn(f"BL v0.1 design={design}{hooks} target=f407 slotA=".encode(), b, bl)
            # SPEC 4: an unrecognised line in update mode is ERR LINE (the host retries on it)
            self.assertNotIn(b"ERR unknown command", b, bl)
            if design == "ab":
                self.assertIn(b"BL resume", b, bl)
            else:
                self.assertNotIn(b"BL resume", b, (bl, "naive must not resume (SPEC 4)"))
            for s in (b"BL nofw", b"BL update", b"BL timeout", b"OK COMMIT",
                      b"OK ERASED ", b"ERR BAD_HDR", b"ERR LINE", b"ERR STATE"):
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
    pos = [text.index(x) for x in parts]
    test.assertEqual(pos, sorted(pos), parts)


class TestSourceContract(unittest.TestCase):
    """C behaviour that stress/sim.py mirrors (stress/tests/test_sim_contract.py pins the sim side)."""

    def src(self, name: str) -> str:
        return re.sub(r"/\*.*?\*/", "", (BOOT / name).read_text(), flags=re.S)

    def test_data_check_order(self):
        body = c_func(self.src("bl_update.c"), "static void cmd_data(")
        in_order(self, body, 'uart_line("ERR LINE")', 'uart_line("ERR STATE")', 'reply_err_off("CRC", off)',
                 'reply_err_off("SEQ", x.written)', 'uart_line("ERR SIZE")', "fault_point(PH_DATA")
        self.assertEqual(body.count('uart_line("ERR LINE")'), 2, "malformed DATA -> ERR LINE")
        self.assertIn("(off & 3u) || off > x.written", body)

    def test_update_mode_unknown_line_and_abort(self):
        body = c_func(self.src("bl_update.c"), "void bl_update_mode(")
        self.assertNotIn("unknown command", body)
        tail = body[body.rindex("} else {"):]
        self.assertIn('uart_line("ERR LINE")', tail)
        abort = body[body.index('str_eq(l, "UPD ABORT")'):body.index('str_eq(l, "PING")')]
        self.assertIn("return;", abort)
        self.assertNotIn("system_reset", abort, "ABORT boots on, it does not reset (SPEC 4)")

    def test_fault_counted_at_expiry(self):
        src = self.src("fault.c")
        isr = c_func(src, "RAMISR void TIM2_IRQHandler(")
        in_order(self, isr, "record_expiry();", "SCB_AIRCR = AIRCR_SYSRESETREQ")
        fp = c_func(src, "uint32_t fault_point(")
        self.assertNotIn("expiry.count", fp)
        self.assertNotIn("fired", fp)
        in_order(self, fp, "noinit_commit();", "if (delay == 0)", "record_expiry();", "timer_start(delay);")
        self.assertNotIn("fired_count", (BOOT / "noinit.h").read_text())
        rec = c_func(src, "static inline __attribute__((always_inline)) void record_expiry(")
        self.assertNotRegex(rec, r"\b(crc32|noinit_commit|uart_\w+)\s*\(", "the ISR must not call flash code")

    def test_timer_delay_one_clamped(self):
        body = c_func(self.src("fault.c"), "static void timer_start(")
        self.assertIn("TIM2_ARR = us > 1u ? us - 1u : 1u;", body)

    def test_boot_snapshot_before_boot_fault_point(self):
        main = c_func(self.src("bl.c"), "int main(")
        in_order(self, main, "noinit_check();", "fault_boot_snapshot();", "fault_point(PH_BOOT, 0)",
                 "print_status();", "fault_print_report();", "boot_decision();")
        snap = c_func(self.src("fault.c"), "void fault_boot_snapshot(")
        self.assertNotIn("count -=", snap, "the snapshot clears nothing")

    def test_expiry_outside_crc(self):
        h = (BOOT / "noinit.h").read_text()
        struct = h[h.index("typedef struct {\n    uint32_t magic;\n    uint32_t enter_update;"):]
        in_order(self, struct, "uint32_t crc32;", "noinit_expiry_t expiry;", "} noinit_t;")
        chk = c_func(self.src("noinit.c"), "int noinit_check(")
        self.assertIn("offsetof(noinit_t, expiry)", chk)


if __name__ == "__main__":
    unittest.main()
