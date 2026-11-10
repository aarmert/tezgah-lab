"""Binary formats: CSV columns vs SPEC, image header, meta records, Intel HEX, sim flash semantics."""
import re
import tempfile
import unittest
import zlib
from pathlib import Path

from stress.csvlog import COLUMNS, CsvLog, fmt, note_kv, read_rows
from stress.images import (ImageSet, build_header, header_error, label_candidates, parse_header, slot_label,
                           slot_valid)
from stress.layout import KIND_NUM, SECTORS, SLOT_SIZE, TRIAL_NONE, payload_addr
from stress.meta import free_index, latest, pack_record, scan_sector, unpack_record
from stress.sim import SimBoard
from stress.simimages import make_golden, make_image, write_image_set
from stress.stlink import bin_to_ihex, ihex_to_segments

SPEC = Path(__file__).resolve().parents[2] / "docs" / "stress" / "SPEC.md"


class Csv(unittest.TestCase):
    def test_columns_match_spec(self):
        m = re.search(r"columns in this order:\n\s+`([^`]+)`", SPEC.read_text(encoding="utf-8"))
        self.assertIsNotNone(m)
        self.assertEqual(list(COLUMNS), m.group(1).split(","))

    def test_write_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.csv"
            log = CsvLog(p)
            log.write({"trial": 0, "outcome": "NEW", "accepted": True, "note": "a=1; b=x; free"})
            with self.assertRaises(KeyError):
                log.write({"nope": 1})
            log.close()
            rows = read_rows(p)
            self.assertEqual(list(rows[0].keys()), list(COLUMNS))
            self.assertEqual((rows[0]["accepted"], rows[0]["outcome"]), ("1", "NEW"))
            self.assertEqual(note_kv(rows[0]["note"]), {"a": "1", "b": "x"})
        self.assertEqual((fmt(None), fmt(False), fmt(1.50)), ("", "0", "1.5"))

    def test_read_legacy_outcomes(self):
        # trials.csv files written before 2026-11-10 carry the Turkish outcome names
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.csv"
            log = CsvLog(p)
            for i, o in enumerate(("YENİ", "ESKİ", "BEKLİYOR", "ÖLÜ", "YANLIŞ", "BELİRSİZ", "NEW")):
                log.write({"trial": i, "outcome": o})
            log.close()
            self.assertEqual([r["outcome"] for r in read_rows(p)],
                             ["NEW", "OLD", "PENDING", "DEAD", "WRONG", "UNCLEAR", "NEW"])


class Header(unittest.TestCase):
    def test_roundtrip_and_checks(self):
        payload = bytes(range(256)) * 4
        h = parse_header(build_header(fw_version=0x020000, security_counter=1, link_addr=payload_addr("B"),
                                      payload=payload))
        self.assertEqual((h.version, h.payload_size, h.payload_crc32), ("2.0.0", 1024, zlib.crc32(payload)))
        self.assertTrue(h.crc_ok())
        self.assertIsNone(header_error(h, "B"))
        self.assertEqual(header_error(h, "A"), "LINK")
        bad = bytearray(h.raw)
        bad[12] ^= 1
        self.assertEqual(header_error(parse_header(bytes(bad)), "B"), "BAD_HDR")
        wrong = parse_header(build_header(fw_version=1, security_counter=1, link_addr=payload_addr("B"),
                                          payload=payload, board_id=0xC0C0C0C0))
        self.assertEqual(header_error(wrong, "B"), "BOARD")
        big = parse_header(build_header(fw_version=1, security_counter=1, link_addr=payload_addr("B"),
                                        payload=payload, payload_size=20000))
        self.assertEqual(header_error(big, "B"), "SIZE")
        self.assertEqual(header_error(None), "BAD_HDR")

    def test_slot_valid_and_labels(self):
        with tempfile.TemporaryDirectory() as td:
            imgs = ImageSet.load(write_image_set(Path(td)))
            v2 = imgs.get("v2", "B")
            slot = v2.data + b"\xff" * (SLOT_SIZE - len(v2.data))
            self.assertTrue(slot_valid(slot, "B")[0])
            self.assertFalse(slot_valid(slot, "A")[0])
            self.assertEqual(slot_label(slot, label_candidates(imgs, "B")), "v2")
            half = v2.data[:600] + b"\xff" * (SLOT_SIZE - 600)
            self.assertEqual(slot_label(half, label_candidates(imgs, "B")), "other")
            self.assertEqual(slot_label(b"\xff" * SLOT_SIZE, label_candidates(imgs, "B")), "empty")
            tr = imgs.get("truncated", "B")
            self.assertEqual(slot_label(tr.data + b"\xff" * (SLOT_SIZE - len(tr.data)),
                                        label_candidates(imgs, "B")), "truncated")
            self.assertEqual(imgs.manifest_problems(), [])
            self.assertEqual(imgs.get("v2crash", "A").version, "2.0.0-crash")
            self.assertEqual(sorted(imgs.golden), ["ab", "ab_metafull", "naive"])


class Meta(unittest.TestCase):
    def test_records(self):
        r = pack_record(7, KIND_NUM["TRIAL"], 0, 1, 0, 1, 3, 2)
        self.assertEqual(len(r), 32)
        m = unpack_record(r, 4, 0)
        self.assertEqual((m.seq, m.kind_name, m.active_slot, m.trial_slot, m.state, m.erases()),
                         (7, "TRIAL", "A", "B", "trial", (2, 3)))
        self.assertIsNone(unpack_record(r[:28] + b"\xff\xff\xff\xff"))     # CRC word not written yet

    def test_scan_latest_free(self):
        s0 = bytearray(b"\xff" * 0x4000)
        s1 = bytearray(b"\xff" * 0x10000)
        s0[0:32] = pack_record(1, 1, 0, TRIAL_NONE, 0, 1, 0, 0)
        s0[32:64] = pack_record(2, 2, 0, 1, 0, 1, 0, 0)
        s0[64:92] = pack_record(3, 3, 0, 1, 1, 1, 0, 0)[:28]             # torn record
        self.assertEqual([r.seq for r in scan_sector(bytes(s0), 1)], [1, 2])
        self.assertEqual(latest(bytes(s0), bytes(s1)).seq, 2)
        self.assertEqual(free_index(bytes(s0)), 3)
        s1[0:32] = pack_record(9, 1, 1, TRIAL_NONE, 0, 1, 1, 0)
        self.assertEqual(latest(bytes(s0), bytes(s1)).seq, 9)
        self.assertIsNone(free_index(b"\x00" * 0x4000))


class IHex(unittest.TestCase):
    def test_roundtrip(self):
        data = make_golden("ab")
        segs = ihex_to_segments(bin_to_ihex(data, 0x08000000))
        self.assertEqual(segs, [(0x08000000, data)])


class SimFlash(unittest.TestCase):
    def setUp(self):
        self.b = SimBoard(seed=1, golden=make_golden("ab"))

    def test_program_only_clears_bits(self):
        op = self.b._program_op(SECTORS[3][0], b"\x0f\xf0\xff\x00")
        op[2]()
        self.assertEqual(self.b.rd(SECTORS[3][0], 4), b"\x0f\xf0\xff\x00")
        op = self.b._program_op(SECTORS[3][0], b"\xf0\xf0\x00\xff")
        op[2]()
        self.assertEqual(self.b.rd(SECTORS[3][0], 4), b"\x00\xf0\x00\x00")

    def test_partial_erase_is_garbage(self):
        a = SECTORS[2][0]
        before = self.b.rd(a, 0x4000)
        op = self.b._erase_op(2)
        op[3](0.5)
        after = self.b.rd(a, 0x4000)
        self.assertNotEqual(after, before)
        self.assertNotEqual(after, b"\xff" * 0x4000)
        self.assertTrue(all((x & y) == y for x, y in zip(after, before)))   # erase only sets bits

    def test_partial_program(self):
        a = SECTORS[3][0]
        data = bytes(range(64))
        op = self.b._program_op(a, data)
        op[3](0.5)
        got = self.b.rd(a, 64)
        self.assertEqual(got[:32], data[:32])
        self.assertEqual(got[36:], b"\xff" * 28)

    def test_boot_and_reset_mid_erase(self):
        b = self.b
        b.advance(b.now + 50_000)
        out = b.host_read(0.1)
        self.assertIn("BOOT app 1.0.0 slot=A trial=0", out)
        b.host_write(b"UPD\r\n")
        b.advance(b.now + 50_000)
        self.assertIn("BL update", b.host_read(0.1))
        b.host_write(b"FAULT ARM 0 ERASE -1 100000\r\n")
        import base64
        hdr = make_image("v2", "B")[:512]
        b.host_write(b"UPD BEGIN " + base64.b64encode(hdr) + b"\r\n")
        b.advance(b.now + 1_000_000)
        out = b.host_read(0.1)
        self.assertIn("BL fault_fired 1 ERASE 3 100000", out)
        self.assertIn("BL resume", out)
        self.assertFalse(slot_valid(b.slot("B"), "B")[0])
        self.assertTrue(slot_valid(b.slot("A"), "A")[0])


if __name__ == "__main__":
    unittest.main()
