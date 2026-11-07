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


# --- calibration ------------------------------------------------------------------
def calibration_path(runs_dir: Path, sim: bool) -> Path:
    return Path(runs_dir) / ("calibration_sim.json" if sim else "calibration.json")


def load_calibration(runs_dir: Path, sim: bool, design: str) -> dict:
    p = calibration_path(runs_dir, sim)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if design in data and "seen" in data[design]:
            return data[design]
        if design in data:
            raise BenchError("calibration_stale", f"The calibration for design={design} in {p} has no scenarios "
                             "(made by an older version).",
                             [f"Run: python -m stress --json calibrate --design {design}" + (" --sim" if sim else "")])
    raise BenchError("calibration_missing", f"No calibration for design={design} in {p}.",
                     [f"Run: python -m stress --json calibrate --design {design}" + (" --sim" if sim else "")])


def save_calibration(runs_dir: Path, sim: bool, design: str, cal: dict) -> Path:
    p = calibration_path(runs_dir, sim)
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    cal = dict(cal, created=dt.datetime.now().isoformat(timespec="seconds"))
    data[design] = cal
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return p


def build_calibration(traces: list[list[tuple[str, int, int]]], windows_us: list[int],
                      update_s: list[float], design: str, sim: bool, manifest_sha: str | None,
                      scenarios: list[str] | None = None) -> dict:
    """traces: per calibration update, the (PHASE, idx, us) lines; scenarios: the scenario of each
    (default normal). Durations are the max seen over all of them. `seen` lists, per scenario, the
    fault points that occurred: only those are swept or drawn for that scenario. DATA indices are
    limited to the chunk count seen in every run of the scenario (v1 and v2 differ in size)."""
    scenarios = scenarios or ["normal"] * len(traces)
    phases: dict[str, dict[str, int]] = {}
    chunks: dict[str, list[int]] = {}
    seen: dict[str, set] = {}
    for sc, tr in zip(scenarios, traces):
        chunks.setdefault(sc, []).append(1 + max((i for p, i, _ in tr if p == "DATA"), default=-1))
        seen.setdefault(sc, set()).update((p, i) for p, i, _ in tr)
        for p, i, us in tr:
            d = phases.setdefault(p, {})
            d[str(i)] = max(us, d.get(str(i), 0))
    return {"design": design, "sim": sim, "manifest_sha256": manifest_sha, "runs": len(traces),
            "phases": phases, "data_chunks_common": min(chunks.get("normal", [0])),
            "data_chunks": {sc: min(c) for sc, c in chunks.items()},
            "seen": {sc: sorted([p, i] for p, i in pts) for sc, pts in seen.items()},
            "update_window_us": max(windows_us) if windows_us else 0,
            "update_s": update_s}


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


def phase_targets(cal: dict, scenario: str = "normal") -> list[tuple[str, int, int]]:
    """(phase, index, duration_us) for every fault point seen in this scenario during calibration."""
    out = []
    seen = seen_points(cal, scenario)
    for phase in PHASES:
        idxs = sorted({i for p, i in seen if p == phase})
        if not idxs:
            continue
        if phase in ANY_INDEX_PHASES:
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
                 scenario: str = "normal") -> list[tuple[str, int, int]]:
    """Every phase x index x delay 0..duration (step_us), capped per phase at max_per_phase
    evenly spaced points. Deterministic: same calibration -> same list."""
    step_us = max(1, int(step_us))
    out = []
    targets = phase_targets(cal, scenario)
    for phase in PHASES:
        pts = [(p, i, d) for p, i, dur in targets if p == phase for d in range(0, dur + 1, step_us)]
        out += evenly(pts, max_per_phase)
    return out


def shuffled(points: list, seed: int, test: str) -> list:
    pts = list(points)
    order_rng(seed, test).shuffle(pts)
    return pts


def random_delay(rng: random.Random, cal: dict) -> int:
    """reset_random: uniform over the measured whole-update window (first ERASE point -> COMMIT)."""
    return rng.randint(0, max(1, int(cal.get("update_window_us", 0))))


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
