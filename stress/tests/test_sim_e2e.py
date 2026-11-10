"""End-to-end --sim runs through the CLI (identical host code path as on hardware)."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from stress.cli import main
from stress.csvlog import COLUMNS, read_rows

TMP = None
CFG = None
CFG_SLOW = None


def setUpModule():
    global TMP, CFG, CFG_SLOW
    TMP = Path(tempfile.mkdtemp(prefix="stress_test_"))
    CFG = TMP / "stress.toml"
    CFG.write_text(f'[stress]\nsalt = "t"\nruns_dir = "{(TMP / "runs").as_posix()}"\n', encoding="utf-8")
    # An ST-Link read slower than the BL's 3 s resume wait (finding: resume race)
    CFG_SLOW = TMP / "stress_slow.toml"
    CFG_SLOW.write_text(f'[stress]\nsalt = "t"\nruns_dir = "{(TMP / "runs_slow").as_posix()}"\n'
                        '[sim]\nstlink_read_s = 3.5\n', encoding="utf-8")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def cli(*args, cfg=None) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--json", "--stress-config", str(cfg or CFG), *args])
    return json.loads(out.getvalue())


def rows_of(r: dict) -> list[dict]:
    return read_rows(Path(r["report_dir"]) / "trials.csv")


class SimRuns(unittest.TestCase):
    def run_ok(self, *args):
        r = cli("run", *args, "--sim")
        self.assertTrue(r["ok"], r)
        rows = rows_of(r)
        self.assertTrue(rows)
        return r, rows

    def test_basic(self):
        _, rows = self.run_ok("basic", "--n", "4")
        self.assertEqual({x["outcome"] for x in rows}, {"NEW"})
        self.assertEqual([x["to_version"] for x in rows], ["2.0.0", "1.0.0", "2.0.0", "1.0.0"])
        self.assertTrue(all(float(x["throughput_Bps"]) > 0 and x["update_s"] for x in rows))

    def test_transfer_cut(self):
        _, rows = self.run_ok("transfer_cut", "--n", "10")
        self.assertEqual({x["outcome"] for x in rows}, {"OLD"})
        self.assertTrue(all(x["byte_offset"] for x in rows))

    def test_line_noise(self):
        _, rows = self.run_ok("line_noise", "--n", "8", "--rate", "0.2")
        self.assertEqual({x["outcome"] for x in rows}, {"NEW"})
        self.assertGreater(sum(int(x["retries"]) for x in rows), 0)

    def test_line_noise_high_rate(self):
        _, rows = self.run_ok("line_noise", "--n", "10", "--rate", "0.3")
        self.assertEqual({x["outcome"] for x in rows}, {"NEW"})

    def test_slow_stlink_resume_race(self):
        """The ST-Link read (3.5 s) outlasts the BL's resume wait (3 s): the host must resume first."""
        r = cli("run", "reset_random", "--n", "12", "--sim", cfg=CFG_SLOW)
        self.assertTrue(r["ok"], r)
        rows = rows_of(r)
        self.assertTrue(all(x["accepted"] == "1" for x in rows), [x["note"] for x in rows])
        self.assertNotIn("DEAD", {x["outcome"] for x in rows})
        self.assertGreater(sum(x["outcome"] == "PENDING" for x in rows), 0)

    def test_reset_tests_ab_all_accepted(self):
        for test, n in (("reset_random", "12"), ("reset_sweep", "30"), ("double_fault", "12")):
            _, rows = self.run_ok(test, "--n", n)
            self.assertTrue(all(x["accepted"] == "1" for x in rows), test)
            self.assertGreater(sum(int(x["fault_fired"] or 0) for x in rows), 0, test)
            self.assertFalse(any("unexpected_reset" in x["note"] for x in rows), test)

    def test_meta_near_full_rotation_swept(self):
        _, rows = self.run_ok("reset_sweep", "--n", "60", "--meta-near-full")
        self.assertTrue(all(x["accepted"] == "1" for x in rows))
        self.assertTrue(all("scenario=metafull" in x["note"] for x in rows))
        rot = [x for x in rows if x["phase"] == "ROTATE"]
        self.assertTrue(rot and all(int(x["fault_fired"]) >= 1 for x in rot), rot)
        self.assertTrue(any(int(x["meta_erases1"] or 0) >= 1 for x in rows))   # it really rotated

    def test_double_fault_second_faults_reachable(self):
        for extra, want in ((["--meta-near-full"], ("ROTATE", "-1")), ([], ("META", "5"))):
            _, rows = self.run_ok("double_fault", "--n", "24", *extra)
            self.assertTrue(all(x["accepted"] == "1" for x in rows), extra)
            hit = [x for x in rows if (x["fault2_phase"], x["fault2_index"]) == want]
            self.assertTrue(hit, (extra, want))
            self.assertTrue(all("fault2=fired" in x["note"] for x in hit), [x["note"] for x in hit])
            self.assertFalse(any((x["fault2_phase"], x["fault2_index"]) == ("META", "6") for x in rows))
        plan = cli("plan", "double_fault", "--n", "4", "--sim")
        self.assertEqual(set(plan["second_faults"]), {"normal", "rollback"})
        self.assertIn("META 6", plan["second_faults"]["normal"]["unreachable"])
        self.assertIn("META 5", plan["second_faults"]["rollback"]["reachable"])
        self.assertIn("ROTATE -1", cli("plan", "double_fault", "--n", "2", "--sim", "--meta-near-full")
                      ["second_faults"]["metafull"]["reachable"])
        bad = cli("run", "basic", "--n", "1", "--sim", "--meta-near-full")
        self.assertEqual(bad["error_type"], "bad_argument")

    def test_bad_image_codes(self):
        _, rows = self.run_ok("bad_image", "--n", "5")
        got = {x["image"].rsplit("_", 1)[0]: x["err_code"] for x in rows}
        self.assertEqual(got, {"bad_crc": "IMG_CRC", "wrong_board": "BOARD", "truncated": "SHORT",
                               "oversize": "SIZE", "old": "VERSION"})
        self.assertEqual({x["outcome"] for x in rows}, {"OLD"})

    def test_rollback(self):
        _, rows = self.run_ok("rollback", "--n", "3")
        self.assertEqual({x["outcome"] for x in rows}, {"OLD"})
        self.assertTrue(all("rollback_s=" in x["note"] for x in rows))
        self.assertEqual({x["attempts"] for x in rows}, {"3"})      # ROLLBACK keeps the attempts

    def test_naive_control_sees_failures(self):
        r = cli("run", "naive_control", "--n", "10", "--sim")
        self.assertTrue(r["ok"], r)
        outcomes = [x["outcome"] for x in rows_of(r)]
        self.assertTrue(any(o in ("DEAD", "WRONG") for o in outcomes), outcomes)
        self.assertTrue(any(x["stlink_needed"] == "1" for x in rows_of(r)))

    def test_naive_sweep_fails_where_ab_passes(self):
        r = cli("run", "reset_sweep", "--n", "40", "--design", "naive", "--sim")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_type"], "test_failed")

    def test_report_and_run_json(self):
        r, rows = self.run_ok("basic", "--n", "2")
        d = Path(r["report_dir"])
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        for k in ("git", "seed", "firmware", "versions", "reproduce", "stlink_serial_hash"):
            self.assertIn(k, run)
        self.assertIn("dirty", run["git"])
        self.assertEqual(len(run["stlink_serial_hash"]), 12)
        (d / "summary.md").unlink()
        rep = cli("report", str(d), "--plot")
        self.assertTrue(rep["ok"], rep)
        self.assertTrue((d / "summary.md").is_file())
        self.assertIn("plots", rep)
        s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(s["failure"]["failures"], 0)
        self.assertAlmostEqual(s["failure"]["upper95"], 1 - 0.05 ** 0.5)
        self.assertTrue((d / "serial.log").read_text(encoding="utf-8").count("UPD BEGIN") >= 2)
        self.assertEqual(list(rows[0].keys()), list(COLUMNS))

    def test_plan_dry_run_doctor(self):
        a = cli("plan", "reset_sweep", "--n", "5", "--seed", "3", "--sim")
        b = cli("plan", "reset_sweep", "--n", "5", "--seed", "3", "--sim")
        self.assertEqual(a["plans"], b["plans"])
        c = cli("plan", "reset_sweep", "--n", "5", "--seed", "4", "--sim")
        self.assertNotEqual(a["plans"], c["plans"])
        d = cli("run", "basic", "--n", "3", "--sim", "--dry-run")
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["plans"]), 3)
        doc = cli("doctor", "--sim")
        self.assertTrue(doc["ok"], doc)

    def test_naive_control_rejects_ab(self):
        r = cli("run", "naive_control", "--design", "ab", "--sim")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_type"], "bad_argument")


if __name__ == "__main__":
    unittest.main()
