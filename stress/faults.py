"""Seeds (SPEC 8), calibration file, reset_random delays and reset_sweep enumeration."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from pathlib import Path

from ._bench import BenchError
from .layout import PHASES

# Entry 1 of double_fault (SPEC 8, test 5): META ROLLBACK / ATTEMPT / REPAIR / ROTATE / BOOT.
# Only the ones the trial's scenario actually reaches are drawn (second_faults()).
DOUBLE_SECOND = (("META", 5), ("META", 3), ("META", 6), ("ROTATE", -1), ("BOOT", 0))
# Scenarios (SPEC 8): normal v1<->v2 from golden_ab; metafull: baseline golden_ab_metafull (META0 one
# record short of full, so the first post-commit append rotates); rollback: update to v2crash (three
# crashed attempts, then ROLLBACK).
SCENARIOS = ("normal", "metafull", "rollback")
# Phases whose index differs between update directions (target sector / slot); swept with index -1
# ("any"), which matches the first occurrence after arming: the slot erase in BEGIN and the
# post-commit jump.
ANY_INDEX_PHASES = ("ERASE", "JUMP", "ROTATE")


def trial_seed(seed: int, test: str, trial: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}:{test}:{trial}".encode()).digest()[:8], "little")


def trial_rng(seed: int, test: str, trial: int) -> random.Random:
    return random.Random(trial_seed(seed, test, trial))


def order_rng(seed: int, test: str) -> random.Random:
    return random.Random(trial_seed(seed, test, -1))


# ESP reset source (SPEC-ESP32 3): the NRST pulse is armed on a protocol line, so only the points
# that name a line can be swept: ERASE -1 (UPD BEGIN), DATA k, HDR 0 (UPD END; its window runs
# through HDR, META, the reset and the trial boot up to 'APP confirmed').
ESP_PHASES = ("ERASE", "DATA", "HDR")


# --- calibration ------------------------------------------------------------------
def calibration_path(runs_dir: Path, sim: bool, target: str = "f407") -> Path:
    suffix = "" if target == "f407" else f"_{target}"
    return Path(runs_dir) / (f"calibration_sim{suffix}.json" if sim else f"calibration{suffix}.json")


def _cal_hint(design: str, sim: bool, target: str) -> str:
    return (f"Run: python -m stress --json calibrate --design {design}" + (" --sim" if sim else "")
            + (f" --target {target}" if target != "f407" else ""))


def load_calibration(runs_dir: Path, sim: bool, design: str, target: str = "f407") -> dict:
    p = calibration_path(runs_dir, sim, target)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if design in data and "seen" in data[design]:
            return data[design]
        if design in data:
            raise BenchError("calibration_stale", f"The calibration for design={design} in {p} has no scenarios "
                             "(made by an older version).", [_cal_hint(design, sim, target)])
    raise BenchError("calibration_missing", f"No calibration for design={design} in {p}.",
                     [_cal_hint(design, sim, target)])


def save_calibration(runs_dir: Path, sim: bool, design: str, cal: dict, target: str = "f407") -> Path:
    p = calibration_path(runs_dir, sim, target)
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    cal = dict(cal, created=dt.datetime.now().isoformat(timespec="seconds"))
    data[design] = cal
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return p


def build_calibration(traces: list[list[tuple[str, int, int]]], windows_us: list[int],
                      update_s: list[float], design: str, sim: bool, manifest_sha: str | None,
                      scenarios: list[str] | None = None, target: str = "f407",
                      esp_windows: list[dict] | None = None) -> dict:
    """traces: per calibration update, the (PHASE, idx, us) lines; scenarios: the scenario of each
    (default normal). Durations are the max seen over all of them. `seen` lists, per scenario, the
    fault points that occurred: only those are swept or drawn for that scenario. DATA indices are
    limited to the chunk count seen in every run of the scenario (v1 and v2 differ in size)."""
    scenarios = scenarios or ["normal"] * len(traces)
    phases: dict[str, dict[str, int]] = {}
    chunks: dict[str, list[int]] = {}
    seen: dict[str, set] = {}
    erase_total = 0
    for sc, tr in zip(scenarios, traces):
        # a slot erase is one F407 sector, four C031 pages: the ERASE -1 sweep spans all of them
        erase_total = max(erase_total, sum(us for p, _, us in tr if p == "ERASE"))
        chunks.setdefault(sc, []).append(1 + max((i for p, i, _ in tr if p == "DATA"), default=-1))
        # A rotation during a normal calibration run (a nearly full log) is a fluke, not a point
        # normal trials reach: ROTATE belongs to the metafull scenario only.
        seen.setdefault(sc, set()).update((p, i) for p, i, _ in tr if not (sc == "normal" and p == "ROTATE"))
        for p, i, us in tr:
            d = phases.setdefault(p, {})
            d[str(i)] = max(us, d.get(str(i), 0))
    cal = {"design": design, "sim": sim, "manifest_sha256": manifest_sha, "runs": len(traces),
           "target": target, "phases": phases, "data_chunks_common": min(chunks.get("normal", [0])),
           "data_chunks": {sc: min(c) for sc, c in chunks.items()},
           "seen": {sc: sorted([p, i] for p, i in pts) for sc, pts in seen.items()},
           "update_window_us": max(windows_us) if windows_us else 0,
           "erase_total_us": erase_total,
           "update_s": update_s}
    if esp_windows:
        cal["esp"] = merge_esp_windows(esp_windows)
    return cal


def merge_esp_windows(runs: list[dict]) -> dict:
    """Per-run {"ERASE": {"-1": us}, "DATA": {"k": us}, "HDR": {"0": us}, "update_window_us": us}
    -> the max of each over the runs."""
    out: dict = {"update_window_us": 0}
    for r in runs:
        for k, v in r.items():
            if k == "update_window_us":
                out[k] = max(out[k], int(v))
                continue
            d = out.setdefault(k, {})
            for i, us in v.items():
                d[str(i)] = max(int(us), d.get(str(i), 0))
    return out


def esp_windows_from_timeline(timeline: list, chunk: int = 192) -> dict | None:
    """One traced update over the ESP bridge with TIME ON (EspBridgeTransport.timeline): the time
    from the last byte of each protocol line leaving the ESP to the end of its reply (BEGIN ->
    OK ERASED, DATA k -> OK <off>, END -> 'APP confirmed' after the trial boot, or the app banner
    when nothing confirms, e.g. the naive BL), in ESP microseconds. These are the delay ranges a
    `NRST ARM TX` on that line can hit."""
    from .esp_bridge import TimedRx, TimedTx
    win: dict = {"ERASE": {}, "DATA": {}, "HDR": {}}
    t_begin = t_end = t_banner = t_conf = None
    pending = None              # (phase, idx, t_txdone, reply predicate)
    seen_data = set()
    for ev in timeline:
        if isinstance(ev, TimedTx):
            txt = ev.text
            if ev.t_us is None:
                continue
            if txt.startswith("UPD BEGIN "):
                t_begin = ev.t_us
                pending = ("ERASE", -1, ev.t_us, lambda s: s.startswith("OK ERASED") or s.startswith("ERR"))
            elif txt.startswith("UPD DATA "):
                off = txt.split()[2]
                pending = None
                if off not in seen_data and off.isdigit():
                    seen_data.add(off)
                    pending = ("DATA", int(off) // chunk, ev.t_us,
                               lambda s, o=off: s == f"OK {o}" or s.startswith("ERR"))
            elif txt == "UPD END" and t_end is None:
                t_end, pending = ev.t_us, None
        elif isinstance(ev, TimedRx):
            line = ev.text.strip()
            if pending is not None:
                phase, idx, t0, pred = pending
                if pred(line):
                    win[phase][str(idx)] = max(0, ev.t_us - t0)
                    pending = None
            if t_end is not None:
                if line == "OK COMMIT" and t_begin is not None and "update_window_us" not in win:
                    win["update_window_us"] = max(0, ev.t_us - t_begin)
                elif "BOOT app " in line and t_banner is None:
                    t_banner = ev.t_us
                elif line == "APP confirmed" and t_conf is None:
                    t_conf = ev.t_us
    t_stop = t_conf if t_conf is not None else t_banner
    if t_end is not None and t_stop is not None:
        win["HDR"]["0"] = max(0, t_stop - t_end)
    if not win["ERASE"] or not win["HDR"]:
        return None
    return win


def seen_points(cal: dict, scenario: str = "normal") -> set[tuple[str, int]]:
    """Fault points that occurred in this scenario during calibration."""
    seen = cal.get("seen")
    if seen is None:       # older calibration (no scenarios): every traced point but ROTATE, normal only
        if scenario != "normal":
            raise BenchError("calibration_stale", f"The calibration has no '{scenario}' scenario.",
                             ["Re-run: python -m stress --json calibrate"])
        return {(p, int(i)) for p, d in cal.get("phases", {}).items() if p != "ROTATE" for i in d}
    if scenario not in seen:
        raise BenchError("calibration_stale", f"The calibration has no '{scenario}' scenario.",
                         ["Re-run: python -m stress --json calibrate"])
    return {(p, int(i)) for p, i in seen[scenario]}


def data_chunks(cal: dict, scenario: str = "normal") -> int:
    return cal.get("data_chunks", {}).get(scenario, cal.get("data_chunks_common", 0))


def phase_duration(cal: dict, phase: str, idx: int | None = None) -> int:
    d = cal.get("phases", {}).get(phase, {})
    if not d:
        if phase == "ROTATE":        # not seen during calibration: estimate a 64 KB sector erase
            return int(2.2 * phase_duration(cal, "ERASE"))
        return 0
    if idx is None or idx == -1 or str(idx) not in d:
        return max(d.values())
    return d[str(idx)]


def esp_targets(cal: dict, scenario: str = "normal") -> list[tuple[str, int, int]]:
    """--reset-source esp: (phase, index, window_us) per trigger line (ERASE -1 = BEGIN, DATA k,
    HDR 0 = END), from `calibrate --link esp`."""
    esp = cal.get("esp")
    if not esp:
        raise BenchError("calibration_stale", "The calibration has no ESP bridge windows (--reset-source esp).",
                         ["Re-run calibrate over the bridge: python -m stress --json calibrate --link esp"
                          + (f" --target {cal.get('target')}" if cal.get("target", "f407") != "f407" else "")])
    out = [("ERASE", -1, int(max(esp.get("ERASE", {"-1": 0}).values())))]
    n = data_chunks(cal, scenario)
    for k in range(n):
        w = esp.get("DATA", {}).get(str(k))
        if w is not None:
            out.append(("DATA", k, int(w)))
    out.append(("HDR", 0, int(max(esp.get("HDR", {"0": 0}).values()))))
    return out


def phase_targets(cal: dict, scenario: str = "normal", source: str = "hook") -> list[tuple[str, int, int]]:
    """(phase, index, duration_us) for every fault point seen in this scenario during calibration."""
    if source == "esp":
        return esp_targets(cal, scenario)
    out = []
    seen = seen_points(cal, scenario)
    for phase in PHASES:
        idxs = sorted({i for p, i in seen if p == phase})
        if not idxs:
            continue
        if phase == "ERASE":
            # one F407 sector, four C031 pages: index -1 starts at the first, the timer runs on
            out.append((phase, -1, max(phase_duration(cal, phase), int(cal.get("erase_total_us", 0)))))
        elif phase in ANY_INDEX_PHASES:
            out.append((phase, -1, phase_duration(cal, phase)))
        elif phase == "DATA":
            for k in range(data_chunks(cal, scenario)):
                out.append(("DATA", k, phase_duration(cal, "DATA", k)))
        else:
            for idx in idxs:
                out.append((phase, idx, phase_duration(cal, phase, idx)))
    return out


def evenly(points: list, m: int) -> list:
    if len(points) <= m:
        return list(points)
    if m <= 1:
        return points[:1]
    return [points[round(i * (len(points) - 1) / (m - 1))] for i in range(m)]


def sweep_points(cal: dict, step_us: int = 10, max_per_phase: int = 50,
                 scenario: str = "normal", source: str = "hook") -> list[tuple[str, int, int]]:
    """Every phase x index x delay 0..duration (step_us), capped per phase at max_per_phase
    evenly spaced points. Deterministic: same calibration -> same list."""
    step_us = max(1, int(step_us))
    out = []
    targets = phase_targets(cal, scenario, source)
    for phase in PHASES:
        pts = [(p, i, d) for p, i, dur in targets if p == phase for d in range(0, dur + 1, step_us)]
        out += evenly(pts, max_per_phase)
    return out


def shuffled(points: list, seed: int, test: str) -> list:
    pts = list(points)
    order_rng(seed, test).shuffle(pts)
    return pts


def random_delay(rng: random.Random, cal: dict, source: str = "hook") -> int:
    """reset_random: uniform over the measured whole-update window (first ERASE point -> COMMIT).
    esp: from the end of the BEGIN line to OK COMMIT, as the bridge measured it."""
    w = int(cal.get("update_window_us", 0))
    if source == "esp":
        w = int((cal.get("esp") or {}).get("update_window_us") or w)
    return rng.randint(0, max(1, w))


def second_faults(cal: dict, scenario: str = "normal") -> tuple[list, list]:
    """(reachable, unreachable) entries of DOUBLE_SECOND for this scenario."""
    seen = seen_points(cal, scenario)
    reach = [(p, i) for p, i in DOUBLE_SECOND
             if (p, i) in seen or (i == -1 and any(q == p for q, _ in seen))]
    return reach, [x for x in DOUBLE_SECOND if x not in reach]


def double_second(rng: random.Random, cal: dict, scenario: str = "normal") -> tuple[str, int, int]:
    reach, _ = second_faults(cal, scenario)
    if not reach:
        raise BenchError("calibration_invalid", f"No second fault of {DOUBLE_SECOND} occurs in '{scenario}'.",
                         ["Re-run calibrate"])
    phase, idx = rng.choice(reach)
    dur = phase_duration(cal, phase, idx) if phase != "META" else (
        phase_duration(cal, "META", idx) or phase_duration(cal, "META"))
    return phase, idx, rng.randint(0, max(0, dur))
