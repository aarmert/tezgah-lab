"""The tests of SPEC 8: per-trial fault plans, per-trial judgement and the run verdict."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import faults
from .classify import ESKI, UNSAFE, YENI

BAD_IMAGES = (("bad_crc", "IMG_CRC"), ("wrong_board", "BOARD"), ("truncated", "SHORT"),
              ("oversize", "SIZE"), ("old", "VERSION"))
ROLLBACK_VARIANTS = ("v2crash", "v2hang", "v2noconfirm")


@dataclass
class TrialPlan:
    trial: int
    seed: int
    test: str
    trial_seed: int
    fault_type: str = "none"
    entry0: tuple[str, int, int] | None = None      # (PHASE, index, delay_us)
    entry1: tuple[str, int, int] | None = None
    cut_u: float | None = None                      # transfer_cut: fraction of the transfer
    noise_rate: float = 0.0
    new_base: str | None = None                     # None: the other of v1/v2
    expect_err: str | None = None
    trace: bool = False

    def describe(self) -> dict:
        d = {"trial": self.trial, "trial_seed": self.trial_seed, "fault_type": self.fault_type}
        if self.entry0:
            d["entry0"] = list(self.entry0)
        if self.entry1:
            d["entry1"] = list(self.entry1)
        if self.cut_u is not None:
            d["cut_fraction"] = round(self.cut_u, 6)
        if self.noise_rate:
            d["noise_rate"] = self.noise_rate
        if self.new_base:
            d["image"] = self.new_base
        if self.expect_err:
            d["expect_err"] = self.expect_err
        return d


@dataclass
class Opts:
    step_us: int = 10
    max_points_per_phase: int = 50
    rate: float = 0.05


@dataclass
class TestDef:
    name: str
    number: str
    fault: str
    needs_hooks: bool
    needs_cal: bool
    default_n: int | None
    design: str | None = None             # forced design (naive_control)
    notes: list[str] = field(default_factory=list)


TESTS = {t.name: t for t in (
    TestDef("basic", "1", "none, alternates v1<->v2", False, False, 20),
    TestDef("transfer_cut", "2", "host stops at a random byte of the transfer", False, False, 50),
    TestDef("line_noise", "3", "corrupt/drop/delay per line at rate p", False, False, 30),
    TestDef("reset_random", "4a", "ERASE -1 + uniform delay over the update window", True, True, 100),
    TestDef("reset_sweep", "4b", "every phase x index x delay", True, True, None),
    TestDef("double_fault", "5", "entry 0 as 4b + entry 1 at META/ROTATE/BOOT", True, True, 100),
    TestDef("bad_image", "6", "each bad image", False, False, 100),
    TestDef("rollback", "7", "v2crash / v2hang / v2noconfirm", False, False, 150),
    TestDef("naive_control", "8", "4a + 4b on the naive BL", True, True, 60, design="naive"),
)}


def make_plans(test: str, n: int | None, seed: int, cal: dict | None, opts: Opts) -> list[TrialPlan]:
    td = TESTS.get(test) or TestDef(test, "-", "calibration (TRACE ON)", True, False, 4)
    points = []
    if test in ("reset_sweep", "double_fault", "naive_control"):
        points = faults.shuffled(faults.sweep_points(cal, opts.step_us, opts.max_points_per_phase), seed, test)
        if not points:
            from ._bench import BenchError
            raise BenchError("calibration_invalid", "The calibration has no fault points.",
                             ["Re-run calibrate; check that TRACE lines are printed"])
    if n is None:
        n = len(points) if test == "reset_sweep" else td.default_n
    plans = []
    for i in range(n):
        rng = faults.trial_rng(seed, test, i)
        p = TrialPlan(i, seed, test, faults.trial_seed(seed, test, i))
        if test == "basic":
            pass
        elif test == "transfer_cut":
            p.fault_type, p.cut_u = "cut", rng.random()
        elif test == "line_noise":
            p.fault_type, p.noise_rate = "noise", opts.rate
        elif test == "reset_random":
            p.fault_type, p.entry0 = "reset_random", ("ERASE", -1, faults.random_delay(rng, cal))
        elif test == "reset_sweep":
            p.fault_type, p.entry0 = "reset_sweep", tuple(points[i % len(points)])
        elif test == "double_fault":
            p.fault_type = "double"
            p.entry0 = tuple(rng.choice(points))
            p.entry1 = faults.double_second(rng, cal)
        elif test == "bad_image":
            kind, code = BAD_IMAGES[i % len(BAD_IMAGES)]
            p.fault_type, p.new_base, p.expect_err = "bad_image", kind, code
        elif test == "rollback":
            p.fault_type, p.new_base = "rollback", ROLLBACK_VARIANTS[i % len(ROLLBACK_VARIANTS)]
        elif test == "naive_control":
            if i % 2 == 0:
                p.fault_type, p.entry0 = "reset_sweep", tuple(points[(i // 2) % len(points)])
            else:
                p.fault_type, p.entry0 = "reset_random", ("ERASE", -1, faults.random_delay(rng, cal))
        elif test == "calibrate":
            p.fault_type, p.trace = "calibrate", True
        plans.append(p)
    return plans


def judge(test: str, r: dict) -> tuple[bool, str]:
    """r: the trial facts (outcome, accepted, smoke_ok, err_code, active_unchanged, trial_jumps, rollbacks)."""
    o = r["outcome"]
    if test == "basic":
        if o != YENI:
            return False, f"expected YENİ, got {o}"
        return (True, "") if r.get("smoke_ok") else (False, "smoke (PING/VER/TICK/UID) failed")
    if test == "transfer_cut":
        if o != ESKI:
            return False, f"expected ESKİ, got {o}"
        return (True, "") if r.get("active_unchanged") else (False, "active slot hash changed")
    if test == "line_noise":
        return (o == YENI), ("" if o == YENI else f"expected YENİ, got {o}")
    if test == "bad_image":
        want = r.get("expect_err")
        if r.get("err_code") != want:
            return False, f"expected ERR {want}, got '{r.get('err_code') or 'no error'}'"
        if o != ESKI:
            return False, f"expected ESKİ, got {o}"
        return (True, "") if r.get("active_unchanged") else (False, "active slot hash changed")
    if test == "rollback":
        if o != ESKI:
            return False, f"expected ESKİ (rolled back), got {o}"
        if r.get("trial_jumps", 0) > 3:
            return False, f"{r['trial_jumps']} trial attempts (> 3)"
        if not r.get("rollbacks"):
            return False, "no 'BL rollback' line"
        return True, ""
    if test == "calibrate":
        return (o == YENI), ""
    # reset_random, reset_sweep, double_fault, naive_control
    return bool(r["accepted"]), ("" if r["accepted"] else f"{o} is not accepted")


def verdict(test: str, rows: list[dict]) -> tuple[bool, str]:
    n = len(rows)
    if n == 0:
        return False, "no trials ran"
    bad = sum(1 for r in rows if str(r.get("expect_ok")) not in ("1", "True"))
    if test == "naive_control":
        unsafe = sum(1 for r in rows if r.get("outcome") in UNSAFE)
        if unsafe:
            return True, f"naive control showed {unsafe}/{n} unsafe outcomes (ÖLÜ/YANLIŞ): the test can see failures"
        return False, (f"naive control showed NO unsafe outcome in {n} trials: the fault injection "
                       "measures nothing (or the naive BL is not naive)")
    if bad == 0:
        return True, f"{n}/{n} trials met the pass criterion"
    return False, f"{bad}/{n} trials failed the pass criterion"
