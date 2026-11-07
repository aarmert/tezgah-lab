"""python -m stress [--json] <run|calibrate|report|plan|doctor> ..."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import VERSION, faults
from ._bench import ROOT, BenchError, bench
from .config import StressConfig, load_stress_config
from .csvlog import CsvLog, Wear
from .images import ImageSet, header_error
from .link import Link, RealClock
from .report import write_summary
from .runner import Ctx, run_trial
from .stlink import Probe
from .tests_defs import BAD_IMAGES, ROLLBACK_VARIANTS, TESTS, Opts, TrialPlan, make_plans, trial_scenario

REQUIRED_BASES = ("v1", "v2", *ROLLBACK_VARIANTS, *(k for k, _ in BAD_IMAGES))


# --- bench session --------------------------------------------------------------------
@dataclass
class Bench:
    ctx: Ctx
    board: object
    bench_name: str
    programmer: str
    programmer_version: str
    probe_serial: str | None
    stack: contextlib.ExitStack


def sim_images(scfg: StressConfig) -> ImageSet:
    from .simimages import write_image_set
    return ImageSet.load(write_image_set(scfg.runs_dir / "sim_images"))


def load_images(scfg: StressConfig, sim: bool, design: str) -> ImageSet:
    images = sim_images(scfg) if sim else ImageSet.load(scfg.images_dir)
    missing = [f"{b}_{s}.img" for b in ("v1", "v2") for s in "AB" if not images.has(b, s)]
    if design not in images.golden:
        missing.append(scfg.golden_name(design))
    if missing:
        raise BenchError("images_missing", f"Missing in {images.dir}: {', '.join(missing)}",
                         ["Build the boot images (firmware/boot, mkimage.py)",
                          "Set [stress] images_dir in stress.toml"])
    return images


def open_bench(args, scfg: StressConfig, design: str, test: str, log_path: Path | None, seed: int) -> Bench:
    stack = contextlib.ExitStack()
    try:
        images = load_images(scfg, args.sim, design)
        if args.sim:
            from .sim import SimBoard, SimClock, SimProgrammer, SimSerial, timings_from
            board = SimBoard(seed=seed, golden=images.golden[design].read_bytes(),
                             timings=timings_from(scfg.data.get("sim", {})))
            transport, clock, prog = SimSerial(board), SimClock(board), SimProgrammer(board)
            bench_name, probe_serial = "sim", "sim"
            wear_path = scfg.runs_dir / "wear_sim.json"
        else:
            board = None
            cfg = bench.load_config(args.config, False)
            stack.enter_context(bench.BenchLock(cfg))
            prog = bench.make_programmer(cfg, None)
            if prog.name != "openocd":
                raise BenchError("not_supported", f"stress needs the OpenOCD backend (dump_image), not {prog.name}.",
                                 ['Set [probe] backend = "openocd" in bench.toml'])
            transport = bench.SerialTransport(cfg.get("serial", "port"), int(cfg.get("serial", "baud")))
            stack.callback(transport.close)
            clock = RealClock()
            bench_name = cfg.get("bench", "name")
            probe_serial = bench.probe_serial(cfg)
            wear_path = scfg.runs_dir / "wear.json"
        log = stack.enter_context(open(log_path, "a", encoding="utf-8")) if log_path else None
        link = Link(transport, clock, log)
        probe = Probe(prog, scfg.runs_dir / ".cache")
        uid = "sim" if args.sim else None
        if not args.sim:
            try:
                uid = probe.read_uid()
            except BenchError:
                uid = None
        wear = Wear(wear_path, scfg.hash_id(uid) or "unknown")
        ctx = Ctx(scfg, images, design, link, probe, wear, test,
                  baseline=getattr(args, "baseline", "any") or "any",
                  observe_s=getattr(args, "observe_s", None) or scfg.observe_s(test),
                  resume_limit_s=getattr(args, "resume_limit_s", None) or float(scfg.get("stress", "resume_limit_s")),
                  settle_s=float(scfg.get("stress", "settle_s")),
                  max_retries=int(scfg.get("stress", "max_retries")),
                  uid=uid if uid != "sim" else None,
                  versions={i.base: i.version for i in images.images.values()})
        return Bench(ctx, board, bench_name, prog.name, prog.tool_version(), probe_serial, stack)
    except BaseException:
        stack.close()
        raise


def git_info() -> dict:
    def g(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return {"commit": g("rev-parse", "HEAD") or None, "dirty": bool(g("status", "--porcelain"))}


def new_run_dir(scfg: StressConfig, name: str) -> Path:
    base = scfg.runs_dir / f"{name}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d, i = base, 2
    while d.exists():
        d = Path(f"{base}_{i}")
        i += 1
    d.mkdir(parents=True)
    return d


def ensure_calibration(args, scfg: StressConfig, design: str) -> dict:
    try:
        return faults.load_calibration(scfg.runs_dir, args.sim, design)
    except BenchError:
        if not args.sim:
            raise
    calibrate(args, scfg, design, runs=4, quiet=True)
    return faults.load_calibration(scfg.runs_dir, True, design)


def reproduce_cmd(args, test: str, n: int, design: str) -> str:
    c = f"python -m stress --json run {test} --n {n} --seed {args.seed} --design {design}"
    if args.sim:
        c += " --sim"
    for k in ("baseline", "step_us", "max_points_per_phase", "rate"):
        v = getattr(args, k, None)
        if v is not None:
            c += f" --{k.replace('_', '-')} {v}"
    if getattr(args, "meta_near_full", False):
        c += " --meta-near-full"
    return c


# --- commands --------------------------------------------------------------------------
def resolve_design(test: str, args) -> str:
    forced = TESTS[test].design if test in TESTS else None
    if forced and args.design and args.design != forced:
        raise BenchError("bad_argument", f"{test} always runs on design={forced}.")
    return forced or args.design or "ab"


def opts_from(args, scfg: StressConfig, design: str = "ab") -> Opts:
    mf = bool(getattr(args, "meta_near_full", False))
    if mf and design != "ab":
        raise BenchError("bad_argument", "--meta-near-full needs design=ab (the naive BL has no meta log).")
    return Opts(step_us=args.step_us or 10, max_points_per_phase=args.max_points_per_phase or 50,
                rate=args.rate if args.rate is not None else float(scfg.get("stress", "line_noise_rate")),
                meta_near_full=mf, design=design)


def second_fault_table(cal: dict, test: str, opts: Opts) -> dict:
    """Per scenario of this double_fault run: which second faults occur (and are drawn) and which do not."""
    out = {}
    for sc in sorted({trial_scenario(test, i, opts) for i in range(2)}):
        reach, unreach = faults.second_faults(cal, sc)
        out[sc] = {"reachable": [f"{p} {i}" for p, i in reach], "unreachable": [f"{p} {i}" for p, i in unreach]}
    return out


def cmd_plan(args, scfg: StressConfig) -> dict:
    test = args.test
    design = resolve_design(test, args)
    opts = opts_from(args, scfg, design)
    cal = ensure_calibration(args, scfg, design) if TESTS[test].needs_cal else None
    plans = make_plans(test, args.n, args.seed, cal, opts)
    listed = [p.describe() for p in plans]
    if not args.json:
        for d in listed[:args.limit or None]:
            print(json.dumps(d, ensure_ascii=False))
    out = {"test": test, "design": design, "n": len(plans), "seed": args.seed}
    if cal:
        out["scenarios"] = {sc: sum(p.scenario == sc for p in plans) for sc in sorted({p.scenario for p in plans})}
        out["sweep_points"] = {sc: len(faults.sweep_points(cal, opts.step_us, opts.max_points_per_phase, sc))
                               for sc in out["scenarios"]}
        if test == "double_fault":
            out["second_faults"] = second_fault_table(cal, test, opts)
        out["calibration"] = {k: cal.get(k) for k in ("created", "sim", "manifest_sha256",
                                                      "update_window_us", "data_chunks_common")}
    return bench.ok_result("plan", f"{test}: {len(plans)} trials planned (seed {args.seed}, design {design}).",
                           plans=listed[:args.limit or None], **out)


def cmd_run(args, scfg: StressConfig) -> dict:
    test = args.test
    td = TESTS[test]
    design = resolve_design(test, args)
    if args.design is None:
        args.design = design
    opts = opts_from(args, scfg, design)
    cal = ensure_calibration(args, scfg, design) if td.needs_cal else None
    plans = make_plans(test, args.n, args.seed, cal, opts)
    if opts.meta_near_full and "ab_metafull" not in load_images(scfg, args.sim, design).golden:
        raise BenchError("images_missing", "golden_ab_metafull.bin is missing.",
                         ["Rebuild: python3 firmware/boot/build_boot.py --json"])
    if args.dry_run:
        load_images(scfg, args.sim, design)
        return bench.ok_result("run", f"[dry-run] {test}: {len(plans)} trials on design={design}; nothing touched.",
                               plans=[p.describe() for p in plans], observe_s=args.observe_s or scfg.observe_s(test))
    run_dir = new_run_dir(scfg, test)
    b = open_bench(args, scfg, design, test, run_dir / "serial.log", args.seed)
    rows, error, interrupted = [], None, False
    run_info = {
        "test": test, "number": td.number, "design": design, "seed": args.seed, "n": len(plans),
        "sim": bool(args.sim), "args": {k: v for k, v in vars(args).items() if k not in ("func",)},
        "started": dt.datetime.now().isoformat(timespec="seconds"), "git": git_info(),
        "versions": {"python": platform.python_version(), "stress": VERSION, "bench": bench.VERSION,
                     "pyserial": _pyserial_version(), "programmer": b.programmer,
                     "programmer_version": b.programmer_version},
        "firmware": {"images_dir": str(b.ctx.images.dir), "manifest_sha256": b.ctx.images.manifest_digest(),
                     "manifest": b.ctx.images.manifest},
        "bench_name": b.bench_name, "board_uid_hash": scfg.hash_id(b.ctx.uid) if b.ctx.uid else None,
        "stlink_serial_hash": scfg.hash_id(b.probe_serial), "salt_set": bool(scfg.salt),
        "observe_s": b.ctx.observe_s, "resume_limit_s": b.ctx.resume_limit_s,
        "calibration": cal, "reproduce": reproduce_cmd(args, test, len(plans), design),
        "scenarios": {sc: sum(p.scenario == sc for p in plans) for sc in sorted({p.scenario for p in plans})},
    }
    if cal and test == "double_fault":
        run_info["second_faults"] = second_fault_table(cal, test, opts)
    (run_dir / "run.json").write_text(json.dumps(run_info, indent=2, ensure_ascii=False), encoding="utf-8")
    csvlog = CsvLog(run_dir / "trials.csv")
    try:
        for p in plans:
            row, _ = run_trial(b.ctx, p)
            csvlog.write(row)
            rows.append(row)
            if not args.json:
                print(f"  trial {p.trial:4d} {row['fault_type']:<12} {row['outcome']:<9} "
                      f"{'ok' if row['expect_ok'] else 'FAIL'}  {row.get('note', '')[:90]}", flush=True)
    except KeyboardInterrupt:
        interrupted = True
    except BenchError as e:
        error = e
    finally:
        csvlog.close()
        b.ctx.link.flush_log()
        run_info.update(finished=dt.datetime.now().isoformat(timespec="seconds"), interrupted=interrupted,
                        trials_done=len(rows), hooks=b.ctx.hooks,
                        board_uid_hash=scfg.hash_id(b.ctx.uid) if b.ctx.uid else None,
                        error=(error.summary if error else None))
        (run_dir / "run.json").write_text(json.dumps(run_info, indent=2, ensure_ascii=False), encoding="utf-8")
        b.stack.close()
    s = write_summary(run_dir)
    extra = {"report_dir": str(run_dir), "counts": {k: v for k, v in s["counts"].items() if v},
             "failure": s["failure"], "verdict": s["verdict"], "trials_done": len(rows)}
    if error:
        return bench.err_result("run", error, **extra)
    if interrupted:
        return {"ok": False, "command": "run", "error_type": "interrupted",
                "summary": f"Interrupted after {len(rows)} trials; partial summary written.",
                "likely_causes": [], **extra}
    ok, why = s["verdict"]["ok"], s["verdict"]["summary"]
    if ok:
        return bench.ok_result("run", f"{test}: PASS. {why}", **extra)
    return bench.err_result("run", BenchError("test_failed", f"{test}: FAIL. {why}",
                                              [f"Read {run_dir / 'summary.md'} and trials.csv (note column)",
                                               f"serial.log in {run_dir}"]), **extra)


def _pyserial_version() -> str | None:
    try:
        import serial
        return serial.__version__
    except ImportError:
        return None


def calibrate(args, scfg: StressConfig, design: str, runs: int, quiet: bool = False) -> dict:
    run_dir = new_run_dir(scfg, f"calibrate_{design}" + ("_sim" if args.sim else ""))
    b = open_bench(args, scfg, design, "calibrate", run_dir / "serial.log", 0)
    traces, windows, update_s, scen = [], [], [], []
    plans = make_plans("calibrate", runs, 0, None, Opts())
    if design == "ab":
        # One update per extra scenario, so the calibration knows which fault points occur there
        # (metafull: ROTATE + the META appends after it; rollback: ATTEMPT x3 + ROLLBACK).
        plans.append(TrialPlan(runs, 0, "calibrate", faults.trial_seed(0, "calibrate", runs),
                               fault_type="calibrate", trace=True, scenario="metafull"))
        plans.append(TrialPlan(runs + 1, 0, "calibrate", faults.trial_seed(0, "calibrate", runs + 1),
                               fault_type="calibrate", trace=True, scenario="rollback", new_base="v2crash"))
    try:
        for p in plans:
            row, ex = run_trial(b.ctx, p)
            want = "ESKİ" if p.scenario == "rollback" else "YENİ"
            if row["outcome"] != want:
                raise BenchError("calibration_failed",
                                 f"Calibration update {p.trial} ({p.scenario}) ended {row['outcome']}, not {want}.",
                                 [f"See {run_dir / 'serial.log'}"])
            tr = [e.data for e in ex["events"] if e.kind == "trace"]
            if not tr:
                raise BenchError("hooks_missing", "No 'T <PHASE> <idx> <us>' lines with FAULT TRACE ON.",
                                 ["Flash the hooks build of the BL and apps"])
            traces.append(tr)
            scen.append(p.scenario)
            if p.scenario != "normal":
                continue
            up = ex["transfer"]
            begin_tx_s = (len("UPD BEGIN ") + 684 + 2) * 10 / 115200
            windows.append(int((up.t_commit - up.t_begin - begin_tx_s) * 1e6))
            if row.get("update_s"):
                update_s.append(row["update_s"])
    finally:
        b.ctx.link.flush_log()
        b.stack.close()
    cal = faults.build_calibration(traces, windows, update_s, design, bool(args.sim),
                                   b.ctx.images.manifest_digest(), scen)
    path = faults.save_calibration(scfg.runs_dir, bool(args.sim), design, cal)
    return {"path": str(path), "calibration": cal, "report_dir": str(run_dir)}


def cmd_calibrate(args, scfg: StressConfig) -> dict:
    design = args.design or "ab"
    r = calibrate(args, scfg, design, args.runs)
    cal = r["calibration"]
    n_pts = len(faults.sweep_points(cal))
    return bench.ok_result("calibrate", f"Calibrated design={design}: {len(cal['phases'])} phases, "
                           f"update window {cal['update_window_us'] / 1e6:.3f} s, {n_pts} default sweep points.",
                           **r)


def cmd_report(args, scfg: StressConfig) -> dict:
    d = Path(args.run_dir)
    if not (d / "trials.csv").is_file():
        raise BenchError("not_found", f"{d / 'trials.csv'} not found.", ["Pass a stress_runs/<test>_<time> folder"])
    s = write_summary(d)
    extra = {"report_dir": str(d), "verdict": s["verdict"], "counts": {k: v for k, v in s["counts"].items() if v}}
    if args.plot:
        from .plot import write_plots
        extra["plots"] = write_plots(d)
    return bench.ok_result("report", f"summary.md rebuilt ({s['n']} trials): {s['verdict']['summary']}", **extra)


def cmd_doctor(args, scfg: StressConfig) -> dict:
    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "required": required})

    add("stress.toml", scfg.path is not None, str(scfg.path) if scfg.path else
        "not found, defaults in use (copy stress.example.toml)", required=False)
    add("salt", bool(scfg.salt), "set" if scfg.salt else "empty: UID/ST-Link hashes are unsalted", required=False)
    images = sim_images(scfg) if args.sim else ImageSet.load(scfg.images_dir)
    add("images_dir", images.dir.is_dir(), str(images.dir))
    missing = [f"{b}_{s}.img" for b in REQUIRED_BASES for s in "AB" if not images.has(b, s)]
    add("images", not missing, "all present" if not missing else "missing: " + ", ".join(missing))
    bad_hdr = []
    for img in images.images.values():
        if img.base in ("v1", "v2", *ROLLBACK_VARIANTS, "old"):
            code = header_error(img.header, img.slot)
            if code:
                bad_hdr.append(f"{img.name}: {code}")
    add("image headers", not bad_hdr, "ok" if not bad_hdr else "; ".join(bad_hdr))
    for design in ("ab", "naive"):
        add(f"golden_{design}", design in images.golden, str(images.golden.get(design, "missing")))
    add("golden_ab_metafull", "ab_metafull" in images.golden,
        str(images.golden.get("ab_metafull", "missing (needed for --meta-near-full)")), required=False)
    probs = images.manifest_problems()
    add("manifest.json", bool(images.manifest) and not probs,
        "hashes match" if images.manifest and not probs else ("missing" if not images.manifest else "; ".join(probs)))
    for design in ("ab", "naive"):
        p = faults.calibration_path(scfg.runs_dir, args.sim)
        try:
            cal = faults.load_calibration(scfg.runs_dir, args.sim, design)
            stale = cal.get("manifest_sha256") != images.manifest_digest()
            add(f"calibration {design}", not stale, f"{p} ({cal.get('created')})" +
                (": STALE, made with other images" if stale else ""), required=False)
        except BenchError:
            add(f"calibration {design}", False, f"none in {p}; run calibrate", required=False)
    if not args.sim:
        try:
            cfg = bench.load_config(args.config, False)
            add("bench.toml", True, str(cfg.path))
            add("probe backend", cfg.get("probe", "backend") == "openocd", cfg.get("probe", "backend"))
            port = cfg.get("serial", "port")
            add("serial port configured", bool(port), port or "empty")
            if not args.no_hardware:
                with bench.BenchLock(cfg):
                    try:
                        prog = bench.make_programmer(cfg, None)
                        add("probe", True, prog.probe_check())
                    except BenchError as e:
                        add("probe", False, e.summary)
                    try:
                        t = bench.SerialTransport(port, int(cfg.get("serial", "baud")))
                        t.close()
                        add("serial open", True, port)
                    except BenchError as e:
                        add("serial open", False, e.summary)
        except BenchError as e:
            add("bench.toml", False, e.summary)
    failed = [c for c in checks if c["required"] and not c["ok"]]
    if not args.json:
        for c in checks:
            mark = "OK  " if c["ok"] else ("FAIL" if c["required"] else "warn")
            print(f"  [{mark}] {c['check']}: {c['detail']}")
    if failed:
        return bench.err_result("doctor", BenchError("doctor_failed", f"{len(failed)} required checks failed.",
                                                     [f"{c['check']}: {c['detail']}" for c in failed]),
                                checks=checks)
    return bench.ok_result("doctor", f"All required checks passed ({len(checks)} checks).", checks=checks)


# --- parser -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m stress", description="Bootloader stress test (docs/stress/SPEC.md)")
    p.add_argument("--json", action="store_true", help="JSON result (ok, error_type, summary, likely_causes)")
    p.add_argument("--config", help="bench.toml path (default: next to bench.py)")
    p.add_argument("--stress-config", help="stress.toml path (default: repo root)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s, n=True):
        s.add_argument("--sim", action="store_true", help="Use the simulator (no hardware)")
        s.add_argument("--design", choices=("ab", "naive"))
        if n:
            s.add_argument("--n", type=int, help="Trials (default per test; reset_sweep: all points)")
            s.add_argument("--seed", type=int, default=1)
            s.add_argument("--step-us", type=int)
            s.add_argument("--max-points-per-phase", type=int)
            s.add_argument("--rate", type=float, help="line_noise per-line fault probability")

    def meta_full(s):
        s.add_argument("--meta-near-full", action="store_true",
                       help="reset_random/reset_sweep/double_fault: baseline golden_ab_metafull before every "
                            "trial (META0 one record short of full: the post-commit append rotates)")

    s = sub.add_parser("run", help="Run a test")
    s.add_argument("test", choices=sorted(TESTS))
    common(s)
    meta_full(s)
    s.add_argument("--baseline", choices=("any", "strict"), default="any")
    s.add_argument("--observe-s", type=float)
    s.add_argument("--resume-limit-s", type=float)
    s.add_argument("--dry-run", action="store_true", help="Plan and check config only; touch nothing")

    s = sub.add_parser("plan", help="Print the deterministic fault list without running")
    s.add_argument("test", choices=sorted(TESTS))
    common(s)
    meta_full(s)
    s.add_argument("--limit", type=int, default=0, help="Print at most this many trials")

    s = sub.add_parser("calibrate", help="TRACE ON updates; writes stress_runs/calibration.json")
    common(s, n=False)
    s.add_argument("--runs", type=int, default=4, help="Updates to trace (alternating directions)")

    s = sub.add_parser("report", help="Rebuild summary.md/json for a run folder")
    s.add_argument("run_dir")
    s.add_argument("--plot", action="store_true")

    s = sub.add_parser("doctor", help="Check images, manifest, calibration, bench config, probe, serial")
    common(s, n=False)
    s.add_argument("--no-hardware", action="store_true", help="Skip opening the probe and serial port")
    return p


COMMANDS = {"run": cmd_run, "plan": cmd_plan, "calibrate": cmd_calibrate, "report": cmd_report,
            "doctor": cmd_doctor}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    for k in ("sim", "design", "n", "seed", "step_us", "max_points_per_phase", "rate"):
        if not hasattr(args, k):
            setattr(args, k, None)
    try:
        scfg = load_stress_config(args.stress_config)
        result = COMMANDS[args.cmd](args, scfg)
    except BenchError as e:
        result = bench.err_result(args.cmd, e)
    except KeyboardInterrupt:
        result = {"ok": False, "command": args.cmd, "error_type": "interrupted", "summary": "Stopped by the user.",
                  "likely_causes": []}
    bench.emit(result, args.json)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
