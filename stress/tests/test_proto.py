import unittest

from stress.proto import err_code, fault_arm, parse_banner, parse_line, parse_status


class Proto(unittest.TestCase):
    def test_status_ab(self):
        s = parse_status("BL v0.1 design=ab hooks slotA=ok:1.0.0 slotB=empty active=A state=confirmed "
                         "attempts=0 seq=12 erases=3,1 reset=PIN,SFT")
        self.assertEqual((s.design, s.hooks, s.slotA, s.slotB, s.active, s.state, s.seq, s.erases, s.reset),
                         ("ab", True, "ok:1.0.0", "empty", "A", "confirmed", 12, (3, 1), "PIN,SFT"))

    def test_status_target_optional(self):
        base = ("BL v0.1 design=ab hooks{} slotA=ok:1.0.0 slotB=empty active=A state=confirmed attempts=0 "
                "seq=3 erases=0,0 reset=SFT,PIN")
        self.assertEqual(parse_status(base.format("")).target, "f407")
        s = parse_status(base.format(" target=f407"))
        self.assertEqual((s.target, s.hooks, s.seq, s.reset), ("f407", True, 3, "SFT,PIN"))
        self.assertEqual(parse_status(base.format(" target=c031")).target, "c031")
        self.assertIsNone(parse_status(base.format(" target=f103")))
        n = parse_status("BL v0.1 design=naive target=f407 slotA=bad slotB=- active=- state=none attempts=0 "
                         "seq=0 erases=0,0 reset=POR")
        self.assertEqual((n.design, n.hooks, n.target), ("naive", False, "f407"))

    def test_status_naive_release(self):
        s = parse_status("BL v0.1 design=naive slotA=bad slotB=- active=- state=none attempts=0 seq=0 "
                         "erases=0,0 reset=POR")
        self.assertEqual((s.design, s.hooks, s.slotB, s.active), ("naive", False, "-", "-"))

    def test_status_rejects(self):
        self.assertIsNone(parse_status("BL v0.2 design=ab slotA=ok:1.0.0 slotB=empty active=A state=none "
                                       "attempts=0 seq=0 erases=0,0 reset=POR"))
        self.assertIsNone(parse_status("BL v0.1 design=xx slotA=ok slotB=empty active=A state=none "
                                       "attempts=0 seq=0 erases=0,0 reset=POR"))

    def test_glued_fragment(self):
        e = parse_line("BOOT apBL v0.1 design=ab slotA=ok:1.0.0 slotB=bad active=A state=trial attempts=2 "
                       "seq=9 erases=0,0 reset=PIN,SFT")
        self.assertEqual(e.kind, "status")
        self.assertTrue(e.data.line.startswith("BL v0.1"))

    def test_banner(self):
        b = parse_banner("BOOT app 2.0.0-crash slot=B trial=1 reset=SFT")
        self.assertEqual((b.version, b.suffix, b.slot, b.trial, b.full_version), ("2.0.0", "crash", "B", True,
                                                                                  "2.0.0-crash"))
        b = parse_banner("BOOT app 1.0.0 slot=A trial=0 reset=IWDG")
        self.assertEqual((b.full_version, b.trial, b.reset), ("1.0.0", False, "IWDG"))
        self.assertIsNone(parse_banner("BOOT stm32bench-fw v1.2.0 reset=PIN"))

    def test_kinds(self):
        cases = {
            "BL jump A": ("jump", ("A", False)), "BL jump B trial": ("jump", ("B", True)),
            "BL rollback B->A": ("rollback", ("B", "A")), "BL repair active=B": ("repair", "B"),
            "BL update": ("wait", "update"), "BL resume": ("wait", "resume"), "BL nofw": ("wait", "nofw"),
            "BL timeout": ("timeout", None), "BL fault_fired 1 DATA 7 120": ("fired", (1, "DATA", 7, 120)),
            "BL fault_fired 2 ERASE -1 0": ("fired", (2, "ERASE", -1, 0)),
            "BL fault_cancelled 1 JUMP 1 900000": ("cancelled", (1, "JUMP", 1, 900000)),
            "APP confirmed": ("confirmed", None), "T ERASE 3 254000": ("trace", ("ERASE", 3, 254000)),
            "OK ERASED 254000": ("reply", ("OK", "ERASED 254000")), "OK 192": ("reply", ("OK", "192")),
            "ERR SEQ 384": ("reply", ("ERR", "SEQ 384")), "OK COMMIT": ("reply", ("OK", "COMMIT")),
            "ERR unknown command": ("reply", ("ERR", "unknown command")), "PONG": ("other", None),
        }
        for line, (kind, data) in cases.items():
            e = parse_line(line)
            self.assertEqual(e.kind, kind, line)
            if data is not None:
                self.assertEqual(e.data, data, line)
        hf = parse_line("HARDFAULT pc=0x08001234 lr=0x080000A7 cfsr=0x00010000 hfsr=0x40000000")
        self.assertEqual(hf.kind, "hardfault")

    def test_err_code_and_fault_arm(self):
        self.assertEqual(err_code("CRC 192"), "CRC")
        self.assertEqual(err_code("IMG_CRC"), "IMG_CRC")
        self.assertEqual(err_code(""), "")
        self.assertEqual(fault_arm(1, "META", 5, 30), "FAULT ARM 1 META 5 30")


if __name__ == "__main__":
    unittest.main()
