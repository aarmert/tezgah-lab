"""NUCLEO-C031C6 target (public/stress/SPEC-C031C6.md): layout, image format, and the simulated C0 BL
answering like SPEC-C031C6 says (pages, double-words, 400-char lines, target=c031)."""
import base64
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path

from stress import layout as L407
from stress._bench import BenchError
from stress.images import ImageSet, build_header, header_error, parse_header, slot_valid
from stress.layout import F407, get
from stress.layout_c031 import LAYOUT as C031
from stress.meta import latest
from stress.proto import parse_status
from stress.sim import SimBoard, timings_from
from stress.simimages import make_bad_image, make_golden, make_image, meta_full_records, write_image_set
from stress.sim import SimProgrammer
from stress.stlink import Probe, regions



def collect(b: SimBoard, secs: float) -> list[str]:
    end = b.now + int(secs * 1e6)
    out = ""
    while b.now < end:
        out += b.host_read((end - b.now) / 1e6)
    return [x for x in out.replace("\r", "").split("\n") if x]


def send(b: SimBoard, line: str, secs: float = 0.3) -> list[str]:
    b.host_write((line + "\r\n").encode("latin-1"))
    return collect(b, secs)


class Layout(unittest.TestCase):
    def test_c031_constants(self):
        self.assertIs(get("c031"), C031)
        self.assertIs(get(None), F407)
        self.assertEqual(C031.header_size, 256)
        self.assertEqual(C031.header_crc_off, 252)
        self.assertEqual(C031.slot_base, {"A": 0x08004000, "B": 0x08006000})
        self.assertEqual(C031.payload_addr("A"), 0x08004100)
        self.assertEqual(C031.slot_size, 8192)
        self.assertEqual(C031.max_payload, 8192 - 256)
        self.assertEqual(C031.meta_region(0), (0x08003000, 0x800))
        self.assertEqual(C031.meta_region(1), (0x08003800, 0x800))
        self.assertEqual(C031.board_id, 0xC031C600)
        self.assertEqual((C031.align, C031.program_unit, C031.chunk), (8, 8, 192))
        self.assertEqual((C031.line_max, C031.begin_max), (400, 400))
        self.assertEqual(C031.ram_top, 0x20003000)
        self.assertEqual(C031.golden_size, 32 * 1024)
        self.assertEqual(C031.bl_size, 12 * 1024)
        self.assertEqual(C031.records_per_meta(), 64)
        self.assertEqual(C031.begin_timeout_s, 1.0)
        self.assertEqual(len("UPD BEGIN ") + C031.header_b64_len, 354)
        self.assertEqual(C031.images_dir, "firmware/boot_c0/build/images")
        with self.assertRaises(BenchError):
            get("g071")

    def test_f407_layout_matches_module_constants(self):
        self.assertEqual(F407.slot_base, L407.SLOT_BASE)
        self.assertEqual(F407.slot_size, L407.SLOT_SIZE)
        self.assertEqual(F407.max_payload, L407.MAX_PAYLOAD)
        self.assertEqual(F407.meta_units, L407.META_SECTORS)
        self.assertEqual(F407.header_b64_len, 684)
        self.assertEqual(regions(F407), {"A": L407.SECTORS[2], "B": L407.SECTORS[3],
                                         "META0": L407.SECTORS[1], "META1": L407.SECTORS[4]})


class ImageFormat(unittest.TestCase):
    def test_header_roundtrip_and_checks(self):
        img = make_image("v1", "A", C031)
        h = parse_header(img, C031)
        self.assertTrue(h.crc_ok())
        self.assertEqual(h.board_id, 0xC031C600)
        self.assertEqual(h.link_addr, 0x08004100)
        self.assertEqual(h.payload_size % 8, 0)
        self.assertIsNone(header_error(h, "A", layout=C031))
        self.assertEqual(header_error(h, "B", layout=C031), "LINK")
        self.assertEqual(img[64:252], b"\xff" * 188)              # fields 0-63, 0xFF up to the CRC
        self.assertEqual(header_error(parse_header(img, F407), "A"), "BAD_HDR")
        payload = img[256:]
        self.assertEqual(payload[:4], (0x20003000).to_bytes(4, "little"))   # SP = C031 RAM top

    def test_payload_multiple_of_8(self):
        p = bytes(100)                                            # 100 % 8 = 4
        h = parse_header(build_header(fw_version=0x10000, security_counter=1, link_addr=0x08004100,
                                      payload=p, layout=C031), C031)
        self.assertEqual(header_error(h, "A", layout=C031), "SIZE")
        self.assertIsNone(header_error(parse_header(build_header(
            fw_version=0x10000, security_counter=1, link_addr=0x08008200, payload=p), F407), "A"))

    def test_bad_images(self):
        wb = parse_header(make_bad_image("wrong_board", "A", C031), C031)
        self.assertEqual(wb.board_id, 0xF407D15C)                 # the F407 v2 app in a C0 header
        self.assertEqual(wb.link_addr, F407.payload_addr("A"))
        self.assertEqual(header_error(wb, "A", layout=C031), "BOARD")
        ov = parse_header(make_bad_image("oversize", "B", C031), C031)
        self.assertEqual(ov.payload_size, 9000)
        self.assertEqual(header_error(ov, "B", layout=C031), "SIZE")

    def test_golden_and_manifest(self):
        g = make_golden("ab", layout=C031)
        self.assertEqual(len(g), 32 * 1024)
        self.assertTrue(slot_valid(g[0x4000:0x6000], "A", C031)[0])
        rec = latest(g[0x3000:0x3800], g[0x3800:0x4000], C031)
        self.assertEqual((rec.seq, rec.sector, rec.meta1), (1, 6, False))
        full = make_golden("ab", meta_full_records(C031), C031)
        self.assertEqual(latest(full[0x3000:0x3800], full[0x3800:0x4000], C031).seq, 63)
        d = Path(tempfile.mkdtemp())
        try:
            imgs = ImageSet.load(write_image_set(d, C031), C031)
            self.assertEqual(imgs.manifest_target, "c031")
            self.assertTrue(all(v["target"] == "c031" for v in imgs.manifest.values()))   # per entry
            self.assertIsNone(imgs.target_problem())
            self.assertIn("c031", ImageSet.load(d, F407).target_problem())
            imgs.manifest["v1_A.img"]["target"] = "f407"
            self.assertEqual(imgs.manifest_target, "mixed")
            self.assertEqual(ImageSet(d, manifest={"target": "c031"}).manifest_target, "c031")
            self.assertEqual(ImageSet(d, manifest={"v1_A.img": {"sha256": "x"}}).manifest_target, "f407")
            self.assertEqual(imgs.get("v2", "B").payload, make_image("v2", "B", C031)[256:])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class C031Board(unittest.TestCase):
    def boot(self, design="ab") -> SimBoard:
        b = SimBoard(seed=3, golden=make_golden(design, layout=C031), layout=C031)
        self.first = collect(b, 0.5)
        return b

    def enter(self, b):
        self.assertIn("BL update", send(b, "UPD", 0.5))

    def begin(self, b, img=None) -> list[str]:
        img = img or make_image("v2", "B", C031)
        return send(b, "UPD BEGIN " + base64.b64encode(img[:256]).decode(), 0.5)

    def test_status_line_target(self):
        self.boot()
        st = parse_status([x for x in self.first if x.startswith("BL v0.1")][0])
        self.assertEqual((st.target, st.design, st.slotA, st.slotB), ("c031", "ab", "ok:1.0.0", "empty"))

    def test_begin_erases_four_pages_one_fault_point(self):
        """firmware/boot_c0/bl_update.c: one ERASE point for the slot, index = its first page."""
        b = self.boot()
        self.enter(b)
        send(b, "FAULT TRACE ON")
        out = self.begin(b)
        tr = [x.split() for x in out if x.startswith("T ERASE")]
        self.assertEqual(len(tr), 1, out)
        self.assertEqual(int(tr[0][2]), 12)
        self.assertTrue(4 * 18_000 < int(tr[0][3]) < 4 * 26_000, tr)
        ok = [x for x in out if x.startswith("OK ERASED")][0]
        self.assertEqual(int(ok.split()[2]), int(tr[0][3]))

    def test_reset_mid_erase_is_per_page(self):
        b = self.boot()
        base = C031.slot_base["B"]
        b.load_segments([(base, b"\x00" * 8192)])
        self.enter(b)
        send(b, "FAULT ARM 0 ERASE -1 33000")              # 1.5 pages into the 4-page erase
        out = self.begin(b)
        self.assertFalse(any(x.startswith("OK ERASED") for x in out), out)
        self.assertTrue(any(x.startswith("BL fault_fired 1 ERASE 12 33000") for x in out), out)
        pages = [b.sector(n) for n in range(12, 16)]
        self.assertEqual(pages[0], b"\xff" * 2048)
        self.assertNotIn(pages[1], (b"\xff" * 2048, b"\x00" * 2048))
        self.assertEqual(pages[2:], [b"\x00" * 2048] * 2)

    def test_garbage_does_not_restart_inactivity_timer(self):
        """C031: only lines not answered with ERR LINE restart the 3 s timer; the F407 restarts on any."""
        for layout, want_timeout in ((C031, True), (F407, False)):
            b = SimBoard(seed=3, golden=make_golden("ab", layout=layout), layout=layout)
            collect(b, 0.5)
            self.assertIn("BL update", send(b, "UPD", 0.5))
            out = []
            for _ in range(4):
                out += send(b, "HELLO", 0.9)
            self.assertEqual("BL timeout" in out, want_timeout, (layout.target, out))
            b2 = SimBoard(seed=3, golden=make_golden("ab", layout=layout), layout=layout)
            collect(b2, 0.5)
            send(b2, "UPD", 0.5)
            out = []
            for _ in range(4):
                out += send(b2, "PING", 0.9)
            self.assertNotIn("BL timeout", out)

    def test_line_limit_400_and_nul(self):
        b = self.boot()
        self.enter(b)
        self.assertEqual(send(b, "x" * 401), ["ERR LINE"])
        self.assertEqual(send(b, "UPD BEGIN " + "A" * 391), ["ERR LINE"])       # 401 chars, BEGIN too
        self.assertEqual(send(b, "PI\x00NG"), ["ERR LINE"])
        self.assertEqual(send(b, "PING"), ["PONG"])

    def test_data_alignment_is_8(self):
        b = self.boot()
        self.enter(b)
        self.assertTrue(any(x.startswith("OK ERASED") for x in self.begin(b)))
        img = make_image("v2", "B", C031)
        payload = img[256:]

        def data(off, chunk):
            return f"UPD DATA {off} {base64.b64encode(chunk).decode()} {zlib.crc32(chunk):08x}"

        self.assertEqual(send(b, data(0, payload[:12])), ["ERR SEQ 0"])      # length 12: not 8-aligned
        self.assertEqual(send(b, data(0, payload[:192])), ["OK 0"])
        self.assertEqual(send(b, data(196, payload[196:388])), ["ERR SEQ 192"])  # off 196: not 8-aligned
        self.assertEqual(send(b, data(192, payload[192:384])), ["OK 192"])
        self.assertEqual(send(b, data(0, payload[:192])), ["OK 0"])          # idempotent retry

    def test_only_erased_double_words_programmable(self):
        b = self.boot()
        a = C031.slot_base["B"]
        self.assertTrue(b._can_program(a, 16))
        b.mem[a - C031.flash_base + 9] = 0xFE
        self.assertFalse(b._can_program(a, 16))
        self.assertTrue(b._can_program(a, 8))
        self.assertTrue(SimBoard(layout=F407)._can_program(L407.SLOT_BASE["B"], 16))

    def test_hardfault_regs_zero(self):
        """ARMv6-M has no CFSR/HFSR: the C0 prints zeros so the host regex stays the same."""
        b = self.boot()
        out = send(b, "CRASH", 0.3)
        hf = [x for x in out if x.startswith("HARDFAULT")]
        self.assertTrue(hf, out)
        self.assertTrue(hf[0].endswith("cfsr=0x00000000 hfsr=0x00000000"), hf)

    def test_meta_record_crc_double_word_last(self):
        """A reset between the two program operations of a record leaves 24 bytes, CRC erased."""
        b = self.boot()
        rec_addr = C031.meta_region(0)[0] + 32           # record 2 (record 1 is the golden's)
        ops = []
        orig = b._program_op

        def spy(addr, data):
            ops.append((addr, len(data)))
            return orig(addr, data)

        b._program_op = spy
        self.enter(b)
        img = make_image("v2", "B", C031)
        self.begin(b, img)
        for off in range(0, len(img) - 256, 192):
            ch = img[256 + off:256 + off + 192]
            send(b, f"UPD DATA {off} {base64.b64encode(ch).decode()} {zlib.crc32(ch):08x}", 0.05)
        out = send(b, "UPD END", 1.5)
        self.assertIn("OK COMMIT", out)
        meta_ops = [o for o in ops if C031.meta_region(0)[0] <= o[0] < C031.meta_region(1)[0] + 0x800]
        self.assertEqual(meta_ops[:2], [(rec_addr, 24), (rec_addr + 24, 8)])

    def test_timings(self):
        t = timings_from({}, "c031")
        self.assertEqual((t.erase_page_us, t.dword_us), (22_000, 85))
        self.assertEqual(t.program_us(C031), 85)
        self.assertEqual(t.program_us(F407), 16)
        self.assertEqual(t.erase_us(C031, 0x800), 22_000)
        self.assertEqual(timings_from({"dword_us": 90}, "c031").dword_us, 90)

    def test_probe_reads_c031_regions(self):
        b = self.boot()
        st = Probe(SimProgrammer(b), Path(tempfile.gettempdir()) / "c031_probe_cache", C031).read_state()
        self.assertEqual((len(st.A), len(st.META0), len(st.META1)), (8192, 2048, 2048))
        self.assertEqual(st.meta().seq, 1)
        self.assertTrue(st.valid("A"))
        self.assertFalse(st.valid("B"))


if __name__ == "__main__":
    unittest.main()
