import unittest

from stress.classify import (BEKLIYOR, BELIRSIZ, ESKI, OLU, YANLIS, YENI, Expect, Verdict, check_boots, classify,
                             combine_resume, count_boots, summarize)
from stress.proto import parse_line

ST = ("BL v0.1 design=ab hooks slotA=ok:1.0.0 slotB=ok:2.0.0 active=A state=trial attempts=0 seq=5 "
      "erases=0,0 reset=SFT")
EX = Expect("ab", "v1", "v2", "1.0.0", "2.0.0", {"v1": "1.0.0", "v2": "2.0.0", "v2crash": "2.0.0-crash"})
GOOD = {"A": "v1", "B": "v2"}


def ev(*lines):
    return [parse_line(x, i) for i, x in enumerate(lines)]


class Classify(unittest.TestCase):
    def test_yeni(self):
        v = classify(ev(ST, "BL jump B trial", "BOOT app 2.0.0 slot=B trial=1 reset=SFT", "APP confirmed"), GOOD, EX)
        self.assertEqual((v.outcome, v.accepted), (YENI, True))
        self.assertEqual(v.running.slot, "B")

    def test_eski(self):
        v = classify(ev(ST, "BL timeout", "BL jump A", "BOOT app 1.0.0 slot=A trial=0 reset=SFT"), GOOD, EX)
        self.assertEqual((v.outcome, v.accepted), (ESKI, True))

    def test_eski_after_rollback(self):
        ex = Expect("ab", "v1", "v2crash", "1.0.0", "2.0.0-crash", EX.versions)
        lines = [ST, "BL jump B trial", "BOOT app 2.0.0-crash slot=B trial=1 reset=SFT",
                 "HARDFAULT pc=0x08001234 lr=0x08001235 cfsr=0x00010000 hfsr=0x40000000"] * 3
        lines += [ST, "BL rollback B->A", "BL jump A", "BOOT app 1.0.0 slot=A trial=0 reset=SFT"]
        v = classify(ev(*lines), {"A": "v1", "B": "v2crash"}, ex)
        self.assertEqual(v.outcome, ESKI)
        w = summarize(ev(*lines))
        self.assertEqual((w.trial_jumps, w.rollbacks), (3, 1))

    def test_yanlis_jump_to_wrong_slot(self):
        v = classify(ev(ST, "BL jump B trial"), {"A": "v1", "B": "other"}, EX)
        self.assertEqual((v.outcome, v.accepted), (YANLIS, False))

    def test_yanlis_beats_everything_naive(self):
        # naive jumped into a half-written slot and then sits in nofw: still YANLIŞ (rule 1 first)
        ex = Expect("naive", "v1", "v2", "1.0.0", "2.0.0", EX.versions)
        v = classify(ev("BL jump A", "BL nofw"), {"A": "empty", "B": "empty"}, ex)
        self.assertEqual(v.outcome, YANLIS)

    def test_yanlis_banner_disagrees_with_hash(self):
        v = classify(ev("BOOT app 2.0.0 slot=A trial=0 reset=SFT"), GOOD, EX)
        self.assertEqual(v.outcome, YANLIS)
        v = classify(ev("BOOT app 2.0.0-crash slot=B trial=1 reset=SFT"), GOOD, EX)
        self.assertEqual(v.outcome, YANLIS)

    def test_olu(self):
        v = classify(ev(ST, "garbage"), GOOD, EX)
        self.assertEqual((v.outcome, v.accepted), (OLU, False))
        self.assertEqual(classify([], GOOD, EX).outcome, OLU)

    def test_bekliyor_variants(self):
        e = ev(ST, "BL resume")
        self.assertEqual(classify(e, GOOD, EX).outcome, BEKLIYOR)
        self.assertFalse(classify(e, GOOD, EX).accepted)
        self.assertTrue(classify(e, GOOD, EX, resume_ok=True).accepted)
        self.assertFalse(classify(e, GOOD, EX, resume_ok=False).accepted)
        for w in ("update", "nofw"):
            self.assertEqual(classify(ev(ST, f"BL {w}"), GOOD, EX).outcome, BEKLIYOR)

    def test_wait_in_middle_is_not_bekliyor(self):
        v = classify(ev(ST, "BL resume", "BL timeout", "BL jump A", "BOOT app 1.0.0 slot=A trial=0 reset=SFT"),
                     GOOD, EX)
        self.assertEqual(v.outcome, ESKI)

    def test_belirsiz_reset_after_banner(self):
        v = classify(ev(ST, "BL jump A", "BOOT app 1.0.0 slot=A trial=0 reset=SFT", ST), GOOD, EX)
        self.assertEqual((v.outcome, v.accepted), (BELIRSIZ, False))

    def test_belirsiz_wrong_version_matching_hash(self):
        # old image runs but its slot holds new: banner/hash agree for v2? no -> slot A labelled v2
        ex = Expect("ab", "v1", "v2", "1.0.0", "2.0.0", {"v1": "1.0.0", "v2": "2.0.0", "x": "3.0.0"})
        v = classify(ev("BOOT app 3.0.0 slot=A trial=0 reset=SFT"), {"A": "x", "B": "v2"}, ex)
        self.assertEqual(v.outcome, BELIRSIZ)

    def test_hardfault_clears_running(self):
        w = summarize(ev("BOOT app 1.0.0 slot=A trial=0 reset=SFT",
                         "HARDFAULT pc=0x08001234 lr=0x08001235 cfsr=0x00010000 hfsr=0x40000000"))
        self.assertIsNone(w.running)

    def test_confirmed_flag(self):
        w = summarize(ev("BOOT app 2.0.0 slot=B trial=1 reset=SFT", "APP confirmed"))
        self.assertTrue(w.confirmed)


class CombineResume(unittest.TestCase):
    first = Verdict(BEKLIYOR, False, ["BL is in 'resume'; not resumed"])

    def test_ok(self):
        v = combine_resume(self.first, Verdict(YENI, True, ["x"]), True)
        self.assertEqual((v.outcome, v.accepted, v.resume_ok), (BEKLIYOR, True, True))

    def test_late(self):
        v = combine_resume(self.first, Verdict(YENI, True, ["x"]), False)
        self.assertEqual((v.outcome, v.accepted), (BEKLIYOR, False))

    def test_timeout(self):
        v = combine_resume(self.first, None, True)
        self.assertEqual((v.outcome, v.accepted, v.resume_ok), (BEKLIYOR, False, False))

    def test_unsafe_during_resume_wins(self):
        for o in (YANLIS, OLU):
            v = combine_resume(self.first, Verdict(o, False, ["bad"]), True)
            self.assertEqual((v.outcome, v.accepted), (o, False))

    def test_eski_after_resume_not_accepted(self):
        v = combine_resume(self.first, Verdict(ESKI, True, ["old"]), True)
        self.assertEqual((v.outcome, v.accepted), (BEKLIYOR, False))


STS = "BL v0.1 design=ab hooks target=f407 slotA=ok:1.0.0 slotB=bad active=A state=confirmed attempts=0 " \
      "seq=5 erases=0,0 reset={}"


class Boots(unittest.TestCase):
    def test_explained_resets(self):
        lines = ["OK UPD", STS.format("SFT,PIN"), "BL update", "OK ERASED 250000",
                 STS.format("SFT,PIN"), "BL fault_fired 1 ERASE 3 1200", "BL resume", "OK COMMIT",
                 STS.format("SFT,PIN"), "BL fault_cancelled 1 JUMP 1 9000", "BL jump B trial",
                 "BOOT app 2.0.0-crash slot=B trial=1 reset=SFT,PIN",
                 "HARDFAULT pc=0x08001234 lr=0x08001235 cfsr=0x00010000 hfsr=0x40000000",
                 STS.format("SFT,PIN"), "BL jump B trial", STS.format("IWDG,PIN")]
        bc = count_boots(ev(*lines))
        self.assertEqual((bc.boots, bc.explained, bc.unexpected), (5, 5, 0))
        self.assertEqual(bc.causes, {"commands": 2, "hardfault": 1, "fired": 1, "iwdg": 1})

    def test_surplus_boot_is_belirsiz(self):
        lines = ["OK UPD", STS.format("SFT,PIN"), "BL update", "OK COMMIT", STS.format("SFT,PIN"),
                 "BL jump B trial", "BOOT app 2.0.0 slot=B trial=1 reset=SFT,PIN", STS.format("PIN"),
                 "BL jump B trial", "BOOT app 2.0.0 slot=B trial=1 reset=PIN", "APP confirmed"]
        bc = count_boots(ev(*lines))
        self.assertEqual(bc.unexpected, 1)
        v = check_boots(Verdict(YENI, True, ["new"]), bc)
        self.assertEqual((v.outcome, v.accepted), (BELIRSIZ, False))
        self.assertIn("1 unexpected reset", v.reasons[-1])
        for unsafe in (OLU, YANLIS):          # an unsafe outcome stays what it is
            self.assertEqual(check_boots(Verdict(unsafe, False, ["x"]), bc).outcome, unsafe)
        ok = Verdict(ESKI, True, ["old"])
        self.assertIs(check_boots(ok, count_boots(ev("OK UPD", STS.format("SFT,PIN")))), ok)

    def test_deficit_is_fine(self):
        # a BOOT fault that resets before the status line: fewer boots than causes
        bc = count_boots(ev("OK UPD", "BL fault_fired 2 BOOT 0 0", STS.format("SFT,PIN")))
        self.assertEqual((bc.boots, bc.explained, bc.unexpected), (1, 3, 0))


if __name__ == "__main__":
    unittest.main()
