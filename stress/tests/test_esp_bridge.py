"""ESP32-S3 bridge: the host transport (stress/esp_bridge.py), the simulated bridge (stress/sim_esp.py)
and the witness, without hardware (SPEC-ESP32 2, 3)."""
import unittest

from stress._bench import BenchError
from stress.esp_bridge import (EspArmer, EspBridgeTransport, TimedRx, TimedTx, Witness, compare, escape,
                               trigger_line, unescape)
from stress.faults import esp_windows_from_timeline
from stress.layout_c031 import LAYOUT as C031
from stress.link import Link
from stress.sim import SimBoard, SimClock, SimSerial
from stress.sim_esp import SimEspPort
from stress.simimages import make_golden

STATUS = ("BL v0.1 design=ab hooks target=c031 slotA=ok:1.0.0 slotB=empty active=A state=confirmed "
          "attempts=0 seq=1 erases=0,0 reset=PIN")


class FakeRaw:
    """A scripted ESP: records what the host writes, answers PING, returns queued output."""

    def __init__(self, ident="tezgah-bridge"):
        self.written: list[str] = []
        self.queue: list[str] = []
        self.ident = ident
        self.closed = False

    def write(self, data: bytes):
        for ln in data.decode().splitlines():
            self.written.append(ln)
            if ln == "PING":
                self.queue.append(f"ESP PONG {self.ident} 0.1\r\n")
            elif ln.startswith("NRST ARM"):
                self.queue.append("ESP OK ARM\r\n")
            elif ln == "NRST DISARM":
                self.queue.append("ESP OK DISARM\r\n")
            elif ln == "STAT":
                self.queue.append("ESP STAT rx_lines=3 tx_lines=4 fired=1 overruns=0\r\n")

    def read_available(self, timeout):
        out, self.queue = "".join(self.queue), []
        return out

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        self.t += 0.001
        return self.t

    def sleep(self, s):
        self.t += s


def bridge(raw=None) -> tuple[EspBridgeTransport, FakeRaw]:
    raw = raw or FakeRaw()
    return EspBridgeTransport(raw, clock=Clock()), raw


class Transport(unittest.TestCase):
    def test_ping_and_wrong_firmware(self):
        b, raw = bridge()
        self.assertEqual(b.version, "0.1")
        self.assertEqual(raw.written, ["PING"])
        with self.assertRaises(BenchError) as cm:
            bridge(FakeRaw("other-fw"))
        self.assertEqual(cm.exception.error_type, "esp_wrong_firmware")

    def test_no_reply(self):
        class Silent(FakeRaw):
            def write(self, data):
                self.written.append(data.decode())
        with self.assertRaises(BenchError) as cm:
            EspBridgeTransport(Silent(), clock=Clock(), ping_timeout_s=1.0)
        self.assertEqual(cm.exception.error_type, "esp_no_reply")

    def test_write_wraps_lines(self):
        b, raw = bridge()
        raw.written.clear()
        b.write(b"UPD END\r\nPING\r\n")
        b.write(b"UPD DATA 0 AAAA")                         # no newline: a cut line, raw bytes
        b.write(b"bad\x01line\r\n")                         # control byte: raw incl. CR LF
        b.write_raw(b"UPD END\r\n")                         # injected (corrupted) line
        b.write(("x" * 1200 + "\r\n").encode())             # longer than TX_MAX: TXH pieces
        self.assertEqual(raw.written[:5], ["TX UPD END", "TX PING", "TXH " + b"UPD DATA 0 AAAA".hex().upper(),
                                           "TXH " + b"bad\x01line\r\n".hex().upper(),
                                           "TXH " + b"UPD END\r\n".hex().upper()])
        big = raw.written[5:]
        self.assertTrue(all(w.startswith("TXH ") for w in big))
        self.assertEqual(b"".join(bytes.fromhex(w[4:]) for w in big), b"x" * 1200 + b"\r\n")

    def test_read_unwraps_rx_and_routes_esp(self):
        b, raw = bridge()
        raw.queue += ["RX OK 0\r\n", "ESP NRST FIRED 1500 7\r\nESP ARMJITTER 7\r\n",
                      "RX BOOT app 2.0.0 slot=B trial=1 reset=PIN\r\nRX \\x00\\x00BL upd", "ate\r\n",
                      "garbage from the ESP ROM\r\n", "RX a\\x5Cb\r\n"]
        text = ""
        for _ in range(4):
            text += b.read_available(0)
        self.assertEqual(text.splitlines(), ["OK 0", "BOOT app 2.0.0 slot=B trial=1 reset=PIN",
                                             "\x00\x00BL update", "a\\b"])
        self.assertEqual([f.delay_us for f in b.fires], [1500])
        self.assertEqual((b.fires[0].late_us, b.fires[0].jitter_us), (7, 7))
        self.assertEqual(b.unknown, ["garbage from the ESP ROM"])
        self.assertTrue(any(ln == "ESP ARMJITTER 7" for _, ln in b.esp_lines))

    def test_escape_roundtrip(self):
        raw = bytes(range(256))
        e = escape(raw)
        self.assertTrue(all(0x20 <= ord(c) <= 0x7E for c in e))
        self.assertEqual(unescape(e).encode("latin-1"), raw)

    def test_control_commands(self):
        b, raw = bridge()
        b.arm_tx(1234, 2)
        b.arm_rx(50, "OK COMMIT")
        b.disarm()
        self.assertEqual(b.stat(), {"rx_lines": 3, "tx_lines": 4, "fired": 1, "overruns": 0})
        self.assertEqual(raw.written[1:], ["NRST ARM TX 1234 2", "NRST ARM RX 50 OK COMMIT", "NRST DISARM", "STAT"])
        with self.assertRaises(BenchError):
            b.nrst_pulse(5)

    def test_stm_lines_survive_control_waits(self):
        """An RX line that arrives while the host waits for 'ESP OK ARM' is kept for the link."""
        b, raw = bridge()
        raw.queue.append("RX OK ERASED 90000\r\n")
        b.arm_tx(10)
        self.assertEqual(b.read_available(0), "OK ERASED 90000\n")


class SimBridge(unittest.TestCase):
    def setUp(self):
        self.board = SimBoard(seed=4, golden=make_golden("ab", layout=C031), layout=C031)
        self.port = SimEspPort(self.board)
        self.clock = SimClock(self.board)
        self.b = EspBridgeTransport(self.port, clock=self.clock)
        self.link = Link(self.b, self.clock)

    def lines(self, secs=0.5):
        self.link.drain(secs)
        return [e.line for e in self.link.events[self.link.unread_from:]]

    def test_ready_ping_and_rx(self):
        self.assertEqual(self.b.ready_seen, 1)
        out = self.lines()
        self.assertTrue(any("target=c031" in x for x in out), out)
        self.link.send_line("PING")
        self.assertIn("PONG", self.lines(0.1))

    def test_arm_tx_fires_after_the_line(self):
        self.lines()
        self.b.time_on(True)
        self.b.arm_tx(5000, 1)
        self.link.send_line("PING")
        out = self.lines(0.5)
        self.assertEqual(len(self.b.fires), 1)
        f = self.b.fires[0]
        self.assertEqual(f.delay_us, 5000)
        self.assertTrue(1 <= f.late_us <= self.board.T.esp_late_us)
        self.assertTrue(any(x.startswith("BL v0.1") and "reset=PIN" in x for x in out), out)
        tx = [e for e in self.b.timeline if isinstance(e, TimedTx)]
        self.assertEqual(tx[-1].text, "PING")
        self.assertIsNotNone(tx[-1].t_us)
        rx = [e for e in self.b.timeline if isinstance(e, TimedRx) and e.text.startswith("BL v0.1")]
        self.assertTrue(rx and rx[0].t_us > tx[-1].t_us + 5000)

    def test_arm_tx_nth_and_disarm(self):
        self.lines()
        self.b.arm_tx(100, 2)
        self.link.send_line("PING")
        self.lines(0.2)
        self.assertEqual(self.b.fires, [])
        self.b.disarm()
        self.link.send_line("PING")
        self.lines(0.3)
        self.assertEqual(self.b.fires, [])
        self.b.arm_tx(100, 2)
        self.link.send_line("PING")
        self.link.send_line("PING")
        self.lines(0.3)
        self.assertEqual(len(self.b.fires), 1)

    def test_arm_rx_and_pulse(self):
        self.lines()
        self.b.arm_rx(200, "PONG")
        self.link.send_line("PING")
        out = self.lines(0.4)
        self.assertEqual(len(self.b.fires), 1)
        self.assertTrue(any(x.startswith("BL v0.1") for x in out))
        self.b.nrst_pulse(1000)
        self.assertTrue(any(x.startswith("BL v0.1") for x in self.lines(0.4)))
        self.assertEqual(len(self.b.fires), 1)                 # a plain pulse is not an armed fire
        st = self.b.stat()
        self.assertEqual(st["fired"], 1)
        self.assertGreater(st["rx_lines"], 4)

    def test_witness_sees_the_same_lines(self):
        tap = self.board.add_tap()
        w = Witness(SimSerial(self.board, tap))
        w.attach(self.link)
        m, wm = self.link.mark(), w.mark()
        self.link.send_line("RESET")
        self.lines(0.5)
        w.settle(0.1)
        link_lines = [e.line for e in self.link.events[m:]]
        wit = [ln for _, ln in w.lines[wm:]]
        self.assertIsNone(compare(link_lines, wit))
        self.assertTrue(any(x.startswith("BL v0.1") for x in wit))
        self.assertIn("witness_mismatch", compare(link_lines, wit[1:] if wit[0].startswith("BL") else wit[:-1]))


class Helpers(unittest.TestCase):
    def test_compare(self):
        a = [STATUS, "BL jump A", "BOOT app 1.0.0 slot=A trial=0 reset=PIN", "OK 0"]
        self.assertIsNone(compare(a, ["junk" + STATUS, "BOOT app 1.0.0 slot=A trial=0 reset=PIN"]))
        note = compare(a, [STATUS])
        self.assertTrue(note.startswith("witness_mismatch: link 2 vs vcp 1"), note)

    def test_trigger_lines(self):
        self.assertEqual(trigger_line(("ERASE", -1, 5)), ("BEGIN", 0))
        self.assertEqual(trigger_line(("DATA", 7, 5)), ("DATA", 7))
        self.assertEqual(trigger_line(("HDR", 0, 5)), ("END", 0))
        self.assertEqual(trigger_line(("META", 2, 5)), ("END", 0))
        self.assertIsNone(trigger_line(("BOOT", 0, 5)))

    def test_armer_arms_once_on_its_line(self):
        b, raw = bridge()
        arm = EspArmer(b, ("DATA", 2, 777))
        for k, i in (("BEGIN", 0), ("DATA", 0), ("DATA", 1), ("DATA", 2), ("DATA", 2), ("END", 0)):
            arm(k, i)
        self.assertEqual([w for w in raw.written if w.startswith("NRST")], ["NRST ARM TX 777 1"])
        with self.assertRaises(BenchError):
            EspArmer(b, ("JUMP", 0, 1))

    def test_windows_from_timeline(self):
        tl = [TimedTx("tx", "UPD BEGIN AAAA", 1000), TimedRx("T ERASE 12 22000", 80000),
              TimedRx("OK ERASED 88000", 91000),
              TimedTx("tx", "UPD DATA 0 xx 1", 92000), TimedRx("OK 0", 95000),
              TimedTx("tx", "UPD DATA 0 xx 1", 96000), TimedRx("OK 0", 99500),     # retry: first counts
              TimedTx("tx", "UPD DATA 192 xx 1", 100000), TimedRx("OK 192", 102500),
              TimedTx("tx", "UPD END", 110000), TimedRx("OK COMMIT", 140000),
              TimedRx("BOOT app 2.0.0 slot=B trial=1 reset=SFT,PIN", 150000),
              TimedRx("APP confirmed", 1160000)]
        w = esp_windows_from_timeline(tl)
        self.assertEqual(w["ERASE"], {"-1": 90000})
        self.assertEqual(w["DATA"], {"0": 3000, "1": 2500})
        self.assertEqual(w["HDR"], {"0": 1050000})
        self.assertEqual(w["update_window_us"], 139000)
        naive = esp_windows_from_timeline(tl[:-1])
        self.assertEqual(naive["HDR"], {"0": 40000})                  # no confirm: up to the banner
        self.assertIsNone(esp_windows_from_timeline(tl[:3]))


if __name__ == "__main__":
    unittest.main()
