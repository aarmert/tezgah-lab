"""Host paths that a plain --sim run rarely takes: the BL leaving update mode before the resume
starts, an unexplained reset, a failed 'UPD' entry. Same host code as hardware, sim underneath."""
import shutil
import tempfile
import unittest
from pathlib import Path

from stress.classify import UNCLEAR, NEW, Expect
from stress.config import load_stress_config
from stress.csvlog import Wear
from stress.images import ImageSet
from stress.link import Link
from stress.runner import Ctx, do_resume, run_trial, wait_banner
from stress.sim import SimBoard, SimClock, SimProgrammer, SimSerial
from stress.simimages import write_image_set
from stress.stlink import Probe
from stress.tests_defs import TrialPlan
from stress.updater import Updater, enter_update

TMP = None


def setUpModule():
    global TMP
    TMP = Path(tempfile.mkdtemp(prefix="stress_paths_"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def make_ctx(test: str, board_cls=SimBoard) -> tuple[Ctx, SimBoard]:
    cfg = TMP / "stress.toml"
    cfg.write_text(f'[stress]\nruns_dir = "{(TMP / "runs").as_posix()}"\n', encoding="utf-8")
    scfg = load_stress_config(str(cfg))
    images = ImageSet.load(write_image_set(TMP / "imgs"))
    board = board_cls(seed=5, golden=images.golden["ab"].read_bytes())
    link = Link(SimSerial(board), SimClock(board))
    ctx = Ctx(scfg, images, "ab", link, Probe(SimProgrammer(board), TMP / "cache"), Wear(TMP / "wear.json", "t"),
              test, versions={i.base: i.version for i in images.images.values()})
    wait_banner(link, 2.0)
    return ctx, board


class ResumeAfterBlTimeout(unittest.TestCase):
    def test_reenters_via_upd(self):
        ctx, board = make_ctx("reset_random")
        link = ctx.link
        old, new = ctx.images.get("v1", "A"), ctx.images.get("v2", "B")
        self.assertTrue(enter_update(link).ok)
        self.assertIsNotNone(link.command("FAULT ARM 0 ERASE -1 0", r"^OK FAULT ARM 0$"))
        up = Updater(link).transfer(new)
        self.assertEqual(up.status, "reset")
        ctx.clock.sleep(4.5)                  # longer than the BL's 3 s resume wait: it boots v1
        ex = Expect("ab", "v1", "v2", "1.0.0", "2.0.0", ctx.versions)
        rr = do_resume(ctx, old, new, ex)
        self.assertEqual(rr.reentered, 1)
        self.assertTrue(any("left update mode" in n for n in rr.notes), rr.notes)
        self.assertEqual(rr.verdict.outcome, NEW)
        # the first transfer sent nothing to the app (no 'ERR line too long')
        self.assertFalse(any("line too long" in e.line for e in link.events))


class StaleReply(unittest.TestCase):
    def test_begin_skips_a_stale_err_state(self):
        """A DATA line that reaches the BL right after a reset gets ERR STATE; BEGIN must not take it."""
        ctx, board = make_ctx("reset_random")
        link = ctx.link
        self.assertTrue(enter_update(link).ok)
        link.send_line("UPD DATA 0 AAAA 00000000")       # no session yet -> ERR STATE, arrives late
        up = Updater(link).transfer(ctx.images.get("v2", "B"))
        self.assertEqual(up.status, "commit", up.notes)
        self.assertTrue(any(e.line == "ERR STATE" for e in link.events))


class Glitch(SimBoard):
    """An external NRST pulse 0.3 s after the new app confirms (no known cause)."""
    glitched = False

    def _print(self, text):
        super()._print(text)
        if text == "APP confirmed" and not self.glitched:
            self.glitched = True
            self.nrst_at = self.now + 300_000


class UnexpectedReset(unittest.TestCase):
    def test_not_accepted(self):
        ctx, board = make_ctx("basic", Glitch)
        row, _ = run_trial(ctx, TrialPlan(0, 1, "basic", 1))
        self.assertTrue(board.glitched)
        self.assertEqual((row["outcome"], row["accepted"], row["expect_ok"]), (UNCLEAR, False, False))
        self.assertIn("unexpected_reset=1", row["note"])
        self.assertTrue(row["stlink_needed"])


class NoUpdateMode(SimBoard):
    """The app answers OK UPD but the BL never sees the enter_update flag (it just boots)."""
    def _app_cmd(self, cmd, s, info, own):
        if cmd == "UPD":
            self._print("OK UPD")
            yield self._flush()
            yield ("reset", "sw")
        else:
            yield from super()._app_cmd(cmd, s, info, own)


class SetupFailure(unittest.TestCase):
    def test_recorded_not_observed(self):
        ctx, _ = make_ctx("reset_random", NoUpdateMode)
        row, _ = run_trial(ctx, TrialPlan(0, 1, "reset_random", 1, entry0=("ERASE", -1, 100)))
        self.assertEqual((row["outcome"], row["accepted"]), (UNCLEAR, False))
        self.assertIn("setup_failed", row["note"])
        self.assertNotIn("DEAD", row["note"])


if __name__ == "__main__":
    unittest.main()
