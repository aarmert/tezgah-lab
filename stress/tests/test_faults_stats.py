import hashlib
import math
import random
import unittest

from stress import faults
from stress.report import betainc, beta_ppf, clopper_pearson, failure_stats, percentile, upper_bound
from stress.tests_defs import Opts, make_plans

_UPD = [["ERASE", 2], ["ERASE", 3], *[["DATA", k] for k in range(35)], ["HDR", 0], ["META", 2], ["BOOT", 0],
        ["JUMP", 0], ["JUMP", 1], ["META", 3]]
CAL = {"phases": {"ERASE": {"2": 250_000, "3": 260_000}, "DATA": {str(k): 768 for k in range(35)},
                  "HDR": {"0": 2048}, "META": {"2": 128, "3": 128, "4": 128, "5": 130}, "BOOT": {"0": 5000},
                  "JUMP": {"0": 20, "1": 20}, "CONFIRM": {"0": 128}, "ROTATE": {"4": 560_000}},
       "seen": {"normal": _UPD + [["CONFIRM", 0], ["META", 4]],
                "metafull": _UPD + [["ROTATE", 4], ["CONFIRM", 0], ["META", 4]],
                "rollback": _UPD + [["META", 5]]},
       "data_chunks": {"normal": 32, "metafull": 32, "rollback": 33},
       "data_chunks_common": 32, "update_window_us": 1_400_000}
OLD_CAL = {k: v for k, v in CAL.items() if k not in ("seen", "data_chunks")}


class Seeds(unittest.TestCase):
    def test_trial_seed_formula(self):
        want = int.from_bytes(hashlib.sha256(b"7:reset_sweep:3").digest()[:8], "little")
        self.assertEqual(faults.trial_seed(7, "reset_sweep", 3), want)
        self.assertNotEqual(faults.trial_seed(7, "reset_sweep", 3), faults.trial_seed(7, "reset_sweep", 4))
        self.assertNotEqual(faults.trial_seed(7, "reset_sweep", 3), faults.trial_seed(8, "reset_sweep", 3))

    def test_plans_deterministic(self):
        for test in ("transfer_cut", "line_noise", "reset_random", "reset_sweep", "double_fault", "bad_image",
                     "rollback", "naive_control", "basic"):
            a = [p.describe() for p in make_plans(test, 12, 5, CAL, Opts())]
            b = [p.describe() for p in make_plans(test, 12, 5, CAL, Opts())]
            self.assertEqual(a, b, test)

    def test_bad_image_and_rollback_cycle(self):
        codes = [p.expect_err for p in make_plans("bad_image", 10, 1, None, Opts())]
        self.assertEqual(codes[:5], ["IMG_CRC", "BOARD", "SHORT", "SIZE", "VERSION"])
        self.assertEqual(codes[5:], codes[:5])
        imgs = [p.new_base for p in make_plans("rollback", 6, 1, None, Opts())]
        self.assertEqual(imgs, ["v2crash", "v2hang", "v2noconfirm"] * 2)


class Sweep(unittest.TestCase):
    def test_targets(self):
        t = faults.phase_targets(CAL)
        self.assertIn(("ERASE", -1, 260_000), t)
        self.assertIn(("JUMP", -1, 20), t)
        self.assertEqual(sum(1 for p, _, _ in t if p == "DATA"), 32)     # common chunk count, not 35
        self.assertIn(("META", 3, 128), t)

    def test_enumeration_and_cap(self):
        pts = faults.sweep_points(CAL, 10, 50)
        by = {}
        for p, i, d in pts:
            by.setdefault(p, []).append((i, d))
        self.assertEqual(len(by["ERASE"]), 50)
        self.assertEqual(by["ERASE"][0], (-1, 0))
        self.assertEqual(by["ERASE"][-1], (-1, 260_000))                  # evenly spaced incl. both ends
        self.assertEqual(len(by["JUMP"]), 3)                              # 0, 10, 20: under the cap
        self.assertEqual(len(by["META"]), 39)                             # 3 kinds x 13 delays
        self.assertEqual(len(by["DATA"]), 50)
        self.assertEqual(pts, faults.sweep_points(CAL, 10, 50))
        self.assertTrue(all(d % 10 == 0 for _, _, d in pts))

    def test_seed_only_shuffles(self):
        pts = faults.sweep_points(CAL)
        a, b = faults.shuffled(pts, 1, "reset_sweep"), faults.shuffled(pts, 2, "reset_sweep")
        self.assertNotEqual(a, b)
        self.assertEqual(sorted(a), sorted(b))
        self.assertEqual(sorted(a), sorted(pts))

    def test_evenly(self):
        self.assertEqual(faults.evenly(list(range(11)), 3), [0, 5, 10])
        self.assertEqual(faults.evenly([1, 2], 5), [1, 2])

    def test_sweep_default_n_is_all_points(self):
        plans = make_plans("reset_sweep", None, 1, CAL, Opts())
        self.assertEqual(len(plans), len(faults.sweep_points(CAL)))
        self.assertEqual(sorted(p.entry0 for p in plans), sorted(faults.sweep_points(CAL)))

    def test_random_and_double(self):
        rng = random.Random(3)
        for _ in range(200):
            d = faults.random_delay(rng, CAL)
            self.assertTrue(0 <= d <= 1_400_000)
            ph, idx, dl = faults.double_second(rng, CAL)
            self.assertIn((ph, idx), faults.DOUBLE_SECOND)
            self.assertGreaterEqual(dl, 0)
        self.assertEqual(faults.phase_duration(OLD_CAL | {"phases": {"ERASE": {"3": 260_000}}}, "ROTATE"),
                         int(2.2 * 260_000))

    def test_scenarios_reachable_points(self):
        norm = {(p, i) for p, i, _ in faults.phase_targets(CAL)}
        full = {(p, i) for p, i, _ in faults.phase_targets(CAL, "metafull")}
        self.assertNotIn("ROTATE", {p for p, _ in norm})
        self.assertIn(("ROTATE", -1), full)
        self.assertEqual(sum(p == "DATA" for p, _, _ in faults.phase_targets(CAL, "rollback")), 33)
        self.assertNotIn(("CONFIRM", 0), {(p, i) for p, i, _ in faults.phase_targets(CAL, "rollback")})
        self.assertEqual(faults.second_faults(CAL, "normal")[0], [("META", 3), ("BOOT", 0)])
        self.assertEqual(faults.second_faults(CAL, "rollback")[0], [("META", 5), ("META", 3), ("BOOT", 0)])
        self.assertEqual(faults.second_faults(CAL, "metafull")[0], [("META", 3), ("ROTATE", -1), ("BOOT", 0)])
        self.assertIn(("META", 6), faults.second_faults(CAL, "metafull")[1])   # REPAIR: never reachable
        # an old calibration (no scenarios) still sweeps the normal scenario, never ROTATE
        self.assertNotIn("ROTATE", {p for p, _, _ in faults.phase_targets(OLD_CAL)})
        with self.assertRaises(Exception):
            faults.phase_targets(OLD_CAL, "metafull")

    def test_double_fault_plans_use_reachable_second(self):
        plans = make_plans("double_fault", 40, 2, CAL, Opts())
        self.assertEqual([p.scenario for p in plans[:4]], ["normal", "rollback"] * 2)
        for p in plans:
            reach = faults.second_faults(CAL, p.scenario)[0]
            self.assertIn(p.entry1[:2], reach)
            self.assertEqual(p.new_base, "v2crash" if p.scenario == "rollback" else None)
        self.assertTrue(any(p.entry1[:2] == ("META", 5) for p in plans))
        full = make_plans("double_fault", 40, 2, CAL, Opts(meta_near_full=True))
        self.assertEqual({p.scenario for p in full}, {"metafull"})
        self.assertTrue(any(p.entry1[:2] == ("ROTATE", -1) for p in full))
        sweep = make_plans("reset_sweep", None, 2, CAL, Opts(meta_near_full=True))
        self.assertTrue(any(p.entry0[0] == "ROTATE" for p in sweep))
        self.assertTrue(all(p.scenario == "metafull" for p in sweep))
        with self.assertRaises(Exception):
            make_plans("basic", 2, 1, CAL, Opts(meta_near_full=True))

    def test_calibration_build(self):
        tr = [[("DATA", k, 700 + k) for k in range(35)] + [("ERASE", 3, 9)],
              [("DATA", k, 800) for k in range(32)] + [("ERASE", 2, 10)]]
        c = faults.build_calibration(tr, [5, 7], [1.0], "ab", True, None)
        self.assertEqual(c["data_chunks_common"], 32)
        self.assertEqual(c["seen"]["normal"][0], ["DATA", 0])
        c2 = faults.build_calibration(tr + [[("ROTATE", 4, 560_000)]], [5, 7], [1.0], "ab", True, None,
                                      ["normal", "normal", "metafull"])
        self.assertEqual(c2["seen"]["metafull"], [["ROTATE", 4]])
        c3 = faults.build_calibration([[("ROTATE", 4, 9), ("BOOT", 0, 5)]], [1], [1.0], "ab", True, None)
        self.assertEqual(c3["seen"]["normal"], [["BOOT", 0]])            # a fluke rotation is not "normal"
        naive = make_plans("double_fault", 4, 1, CAL, Opts(design="naive"))
        self.assertEqual({p.scenario for p in naive}, {"normal"})
        self.assertEqual(c2["data_chunks"], {"normal": 32, "metafull": 0})
        self.assertEqual(c["phases"]["DATA"]["0"], 800)
        self.assertEqual(c["phases"]["DATA"]["34"], 734)
        self.assertEqual(c["update_window_us"], 7)


class Stats(unittest.TestCase):
    def test_zero_failure_bound(self):
        for n in (1, 10, 59, 300, 1000):
            self.assertAlmostEqual(upper_bound(0, n), 1 - 0.05 ** (1 / n), places=12)
            # the CP one-sided formula gives the same at k = 0
            self.assertAlmostEqual(beta_ppf(0.95, 1, n), 1 - 0.05 ** (1 / n), places=9)
        s = failure_stats(0, 300)
        self.assertAlmostEqual(s["rule_of_three"], 0.01)
        self.assertAlmostEqual(s["upper95"], 0.009936, places=5)

    def test_clopper_pearson_known_values(self):
        # reference values (R binom.test / scipy.stats.beta.ppf)
        lo, hi = clopper_pearson(1, 10)
        self.assertAlmostEqual(lo, 0.002528579, places=7)
        self.assertAlmostEqual(hi, 0.445016117, places=7)
        lo, hi = clopper_pearson(5, 10)
        self.assertAlmostEqual(lo, 0.187086028, places=7)
        self.assertAlmostEqual(hi, 0.812913972, places=7)
        lo, hi = clopper_pearson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 1 - 0.025 ** 0.1, places=12)   # closed form at k = 0
        self.assertAlmostEqual(upper_bound(1, 10), 0.3941633024, places=9)
        self.assertEqual(clopper_pearson(10, 10)[1], 1.0)

    def test_clopper_pearson_definition(self):
        # CP bounds are where the binomial tail equals alpha/2: P(X<=k|hi)=0.025, P(X>=k|lo)=0.025
        from math import comb

        def cdf(k, n, p):
            return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
        for n in (10, 37, 120):
            for k in range(1, n, max(1, n // 6)):
                lo, hi = clopper_pearson(k, n)
                self.assertAlmostEqual(cdf(k, n, hi), 0.025, places=9)
                self.assertAlmostEqual(1 - cdf(k - 1, n, lo), 0.025, places=9)
                self.assertAlmostEqual(cdf(k, n, upper_bound(k, n)), 0.05, places=9)
        self.assertEqual(clopper_pearson(10, 10)[1], 1.0)

    def test_betainc(self):
        self.assertAlmostEqual(betainc(2, 3, 0.4), 0.5248, places=10)
        self.assertAlmostEqual(betainc(1, 1, 0.3), 0.3, places=12)
        self.assertAlmostEqual(betainc(3, 5, 0.2) + betainc(5, 3, 0.8), 1.0, places=12)

    def test_percentile(self):
        self.assertIsNone(percentile([], 0.5))
        self.assertEqual(percentile([3, 1, 2], 0.5), 2)
        self.assertAlmostEqual(percentile(list(range(1, 11)), 0.95), 9.55)
        self.assertTrue(math.isclose(percentile([1.0], 0.95), 1.0))


if __name__ == "__main__":
    unittest.main()
