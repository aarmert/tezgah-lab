"""The simulator must answer like the C firmware (firmware/boot/*.c). Each test pins one behaviour
that firmware/boot/tests/test_mkimage.py pins on the C side (TestSourceContract)."""
import base64
import unittest
import zlib

from stress.layout import HEADER_SIZE, SECTORS, TRIAL_NONE
from stress.meta import latest
from stress.proto import parse_line, parse_status
from stress.sim import SimBoard, c_b64, c_hex32, c_u32
from stress.simimages import make_golden, make_image


def collect(b: SimBoard, secs: float) -> list[str]:
    end = b.now + int(secs * 1e6)
    out = ""
    while b.now < end:
        out += b.host_read((end - b.now) / 1e6)
    return [x for x in out.replace("\r", "").split("\n") if x]


def send(b: SimBoard, line: str, secs: float = 0.3) -> list[str]:
    b.host_write((line + "\r\n").encode("latin-1"))
    return collect(b, secs)


def data_line(off: int, chunk: bytes, crc: int | None = None) -> str:
    return f"UPD DATA {off} {base64.b64encode(chunk).decode()} {zlib.crc32(chunk) if crc is None else crc:08x}"


class Board(unittest.TestCase):
    def boot(self, design: str = "ab") -> SimBoard:
        b = SimBoard(seed=3, golden=make_golden(design))
        self.first = collect(b, 0.5)
        return b

    def enter(self, b: SimBoard) -> list[str]:
        out = send(b, "UPD", 0.5)
        self.assertIn("BL update", out)
        return out


class StatusLine(Board):
    def test_target_and_parse(self):
        self.boot()
        st = [x for x in self.first if x.startswith("BL v0.1")][0]
        self.assertIn(" hooks target=f407 slotA=", st)
        s = parse_status(st)
        self.assertEqual((s.design, s.hooks, s.target, s.active), ("ab", True, "f407", "A"))

    def test_reset_flags_in_c_order(self):
        b = self.boot()
        out = send(b, "RESET", 0.5)
        st = parse_status([x for x in out if x.startswith("BL v0.1")][0])
        self.assertEqual(st.reset, "SFT,PIN")


class UpdateProtocol(Board):
    def begin(self, b: SimBoard) -> bytes:
        img = make_image("v2", "B")
        out = send(b, "UPD BEGIN " + base64.b64encode(img[:HEADER_SIZE]).decode(), 1.0)
        self.assertTrue(any(x.startswith("OK ERASED ") for x in out), out)
        return img

    def test_unknown_line_is_err_line(self):
        b = self.boot()
        self.enter(b)
        self.assertEqual(send(b, "HELLO"), ["ERR LINE"])
        self.assertEqual(send(b, "UPD DATX 0 AAAA 0"), ["ERR LINE"])
        self.assertEqual(send(b, "UPD END "), ["ERR LINE"])        # no strip: C compares exactly
        self.assertEqual(send(b, "x" * 301), ["ERR LINE"])

    def test_data_order_line_state_crc_seq_size(self):
        b = self.boot()
        self.enter(b)
        chunk = bytes(range(192))
        self.assertEqual(send(b, "UPD DATA 0"), ["ERR LINE"])                 # malformed
        self.assertEqual(send(b, "UPD DATA 0 AAAA zz"), ["ERR LINE"])
        self.assertEqual(send(b, "UPD DATA 0 AAAA 123456789"), ["ERR LINE"])  # 9 hex digits
        self.assertEqual(send(b, data_line(0, chunk)), ["ERR STATE"])         # no session
        img = self.begin(b)
        payload = img[HEADER_SIZE:]
        self.assertEqual(send(b, "UPD DATA 0 !!!! 00000000"), ["ERR CRC 0"])
        self.assertEqual(send(b, data_line(0, chunk, crc=1)), ["ERR CRC 0"])
        self.assertEqual(send(b, data_line(2, chunk)), ["ERR SEQ 0"])
        # beyond the next expected offset AND past the image size: SEQ wins (checked first)
        self.assertEqual(send(b, data_line(len(payload), b"\0\0\0\0")), ["ERR SEQ 0"])
        self.assertEqual(send(b, data_line(0, b"abc")), ["ERR SIZE"])
        self.assertEqual(send(b, data_line(0, payload[:192])), ["OK 0"])
        self.assertEqual(send(b, data_line(0, payload[:192])), ["OK 0"])      # idempotent retry
        self.assertEqual(send(b, data_line(0, chunk)), ["ERR SEQ 192"])
        # END error ends the session (xfer_fail): the next END is ERR STATE
        self.assertEqual(send(b, "UPD END"), ["ERR SHORT"])
        self.assertEqual(send(b, "UPD END"), ["ERR STATE"])

    def test_abort_boots_on_without_reset(self):
        b = self.boot()
        self.enter(b)
        resets = b.resets
        out = send(b, "UPD ABORT", 0.5)
        self.assertEqual(out[0], "OK ABORT")
        self.assertIn("BL jump A", out)
        self.assertFalse(any(x.startswith("BL v0.1") for x in out), out)
        self.assertEqual(b.resets, resets)

    def test_begin_target_and_trial_record_without_meta(self):
        """No meta + nofw: target A, TRIAL record active=0xFF min_sc=0 (bl_update.c target_slot)."""
        g = bytearray(make_golden("ab"))
        for s in (1, 2, 4):
            o = SECTORS[s][0] - SECTORS[0][0]
            g[o:o + SECTORS[s][1]] = b"\xff" * SECTORS[s][1]
        b = SimBoard(seed=1, golden=bytes(g))
        self.assertIn("BL nofw", collect(b, 0.5))
        img = make_image("v1", "A")
        send(b, "UPD BEGIN " + base64.b64encode(img[:HEADER_SIZE]).decode(), 1.0)
        p = img[HEADER_SIZE:]
        for off in range(0, len(p), 192):
            self.assertEqual(send(b, data_line(off, p[off:off + 192]), 0.1), [f"OK {off}"])
        b.host_write(b"UPD END\r\n")
        b.advance(b.now + 50_000)                                # stop before the trial app confirms
        rec = latest(b.sector(1), b.sector(4))                   # TRIAL, or the ATTEMPT that copies it
        self.assertIn(rec.kind, (2, 3))
        self.assertEqual((rec.active, rec.trial, rec.min_security_counter), (0xFF, 0, 0))

    def test_meta_step_before_update_flag(self):
        """bl.c step 1 (no meta: append CONFIRMED) runs before the enter_update flag check."""
        g = bytearray(make_golden("ab"))
        o = SECTORS[1][0] - SECTORS[0][0]
        g[o:o + SECTORS[1][1]] = b"\xff" * SECTORS[1][1]
        b = SimBoard(seed=1, golden=bytes(g))
        collect(b, 0.5)
        self.enter(b)
        self.assertEqual(latest(b.sector(1), b.sector(4)).kind, 1)

    def test_naive_begin_checks_magic_crc_size_only(self):
        from stress.images import build_header
        from stress.layout import payload_addr
        b = self.boot("naive")
        self.enter(b)
        p = make_image("v2", "A")[HEADER_SIZE:]
        h = build_header(fw_version=0x020000, security_counter=0, link_addr=payload_addr("B"), payload=p,
                         board_id=0xC0C0C0C0)
        out = send(b, "UPD BEGIN " + base64.b64encode(h).decode(), 1.0)
        self.assertTrue(out and out[0].startswith("OK ERASED"), out)

    def test_rollback_keeps_attempts(self):
        b = self.boot()
        self.enter(b)
        img = make_image("v2crash", "B")
        send(b, "UPD BEGIN " + base64.b64encode(img[:HEADER_SIZE]).decode(), 1.0)
        p = img[HEADER_SIZE:]
        for off in range(0, len(p), 192):
            send(b, data_line(off, p[off:off + 192]), 0.1)
        out = send(b, "UPD END", 3.0)
        self.assertIn("BL rollback B->A", out)
        rec = latest(b.sector(1), b.sector(4))
        self.assertEqual((rec.kind, rec.trial, rec.attempts), (5, TRIAL_NONE, 3))


class FaultHook(Board):
    def test_fired_only_at_expiry_commit_cancels(self):
        b = self.boot()
        self.enter(b)
        self.assertEqual(send(b, "FAULT ARM 0 ERASE -1 60000000"), ["OK FAULT ARM 0"])
        img = make_image("v2", "B")
        send(b, "UPD BEGIN " + base64.b64encode(img[:HEADER_SIZE]).decode(), 1.0)
        p = img[HEADER_SIZE:]
        for off in range(0, len(p), 192):
            send(b, data_line(off, p[off:off + 192]), 0.1)
        out = send(b, "UPD END", 1.0)
        self.assertIn("OK COMMIT", out)
        self.assertIn("BL fault_cancelled 1 ERASE 3 60000000", out)
        self.assertFalse(any(x.startswith("BL fault_fired") for x in out), out)

    def test_fired_report_and_delay_one(self):
        b = self.boot()
        self.enter(b)
        self.assertEqual(send(b, "FAULT ARM 0 ERASE -1 1"), ["OK FAULT ARM 0"])
        img = make_image("v2", "B")
        out = send(b, "UPD BEGIN " + base64.b64encode(img[:HEADER_SIZE]).decode(), 1.0)
        self.assertIn("BL fault_fired 1 ERASE 3 1", out)          # delay 1 = 2 us, it fires
        self.assertIn("BL resume", out)
        self.assertEqual(parse_line("BL fault_cancelled 1 ERASE 3 1").kind, "cancelled")

    def test_boot_fault_delay0_keeps_earlier_count(self):
        b = self.boot()
        self.enter(b)
        send(b, "FAULT ARM 0 ERASE -1 0")
        send(b, "FAULT ARM 1 BOOT 0 0")
        img = make_image("v2", "B")
        out = send(b, "UPD BEGIN " + base64.b64encode(img[:HEADER_SIZE]).decode(), 1.0)
        self.assertEqual(sum(x.startswith("BL v0.1") for x in out), 1, out)   # the killed boot prints nothing
        self.assertIn("BL fault_fired 2 BOOT 0 0", out)

    def test_clear_cancels_pending_timer(self):
        b = self.boot()
        send(b, "FAULT ARM 0 CONFIRM 0 5000000")
        self.enter(b)          # no CONFIRM point in update mode: still armed
        self.assertEqual(send(b, "FAULT LIST"), ["FAULT 0 CONFIRM 0 5000000", "FAULT 1 -"])
        self.assertEqual(send(b, "FAULT ARM 2 BOOT 0 0"), ["ERR FAULT"])
        self.assertEqual(send(b, "FAULT  CLEAR"), ["ERR FAULT"])   # exactly one space (fault.c)


class AppCommands(Board):
    def test_app_line_limit_and_case(self):
        b = self.boot()
        self.assertEqual(send(b, "UPD BEGIN " + "A" * 684), ["ERR line too long"])
        self.assertEqual(send(b, "ping"), ["PONG"])
        self.assertEqual(send(b, "state"), ["STATE active=A trial=0 confirmed=1 seq=1"])
        self.assertEqual(send(b, "FAULT"), ["ERR FAULT"])
        self.assertEqual(send(b, "FAULTX"), ["ERR unknown command"])

    def test_naive_app_state_reads_meta(self):
        b = self.boot("naive")
        self.assertEqual(send(b, "STATE"), ["STATE active=- trial=0 confirmed=0 seq=0"])


class CParsers(unittest.TestCase):
    def test_b64_like_c(self):
        for n in (0, 1, 2, 3, 191, 192):
            data = bytes(range(n % 256)) * 1
            self.assertEqual(c_b64(base64.b64encode(data).decode(), 512), data)
        for bad in ("A", "AB=C", "A===", "AA=A", "AAAA=AAA", "AA!A"):
            self.assertIsNone(c_b64(bad, 512), bad)
        self.assertIsNone(c_b64(base64.b64encode(b"x" * 193).decode(), 192))

    def test_numbers_like_c(self):
        self.assertEqual(c_u32("12 rest"), (12, "rest"))
        self.assertIsNone(c_u32("4294967296"))
        self.assertIsNone(c_u32("12x"))
        self.assertEqual(c_hex32("deadBEEF"), (0xDEADBEEF, ""))
        self.assertEqual(c_hex32("1f "), (0x1F, ""))
        self.assertIsNone(c_hex32("123456789"))


if __name__ == "__main__":
    unittest.main()
