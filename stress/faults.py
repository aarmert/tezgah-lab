"""Seeds (SPEC 8), calibration file, reset_random delays and reset_sweep enumeration."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from pathlib import Path

from ._bench import BenchError
from .layout import PHASES

# Entry 1 of double_fault (SPEC 8, test 5): META ROLLBACK / ATTEMPT / REPAIR / ROTATE / BOOT
DOUBLE_SECOND = (("META", 5), ("META", 3), ("META", 6), ("ROTATE", -1), ("BOOT", 0))
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
        if design in data:
            return data[design]
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
                      update_s: list[float], design: str, sim: bool, manifest_sha: str | None) -> dict:
    """traces: per calibration update, the (PHASE, idx, us) lines. Durations are the max seen.
    DATA indices are limited to the chunk count seen in every run (v1 and v2 differ in size)."""
    phases: dict[str, dict[str, int]] = {}
    chunks = []
    for tr in traces:
        chunks.append(1 + max((i for p, i, _ in tr if p == "DATA"), default=-1))
        for p, i, us in tr:
            d = phases.setdefault(p, {})
            d[str(i)] = max(us, d.get(str(i), 0))
    return {"design": design, "sim": sim, "manifest_sha256": manifest_sha, "runs": len(traces),
            "phases": phases, "data_chunks_common": min(chunks) if chunks else 0,
            "update_window_us": max(windows_us) if windows_us else 0,
            "update_s": update_s}


def phase_duration(cal: dict, phase: str, idx: int | None = None) -> int:
    d = cal.get("phases", {}).get(phase, {})
    if not d:
        if phase == "ROTATE":        # not seen during calibration: estimate a 64 KB sector erase
            return int(2.2 * phase_duration(cal, "ERASE"))
        return 0
    if idx is None or idx == -1 or str(idx) not in d:
        return max(d.values())
    return d[str(idx)]


def phase_targets(cal: dict) -> list[tuple[str, int, int]]:
    """(phase, index, duration_us) for every fault point seen during calibration."""
    out = []
    ph = cal.get("phases", {})
    for phase in PHASES:
        if phase not in ph:
            continue
        if phase in ANY_INDEX_PHASES:
            out.append((phase, -1, phase_duration(cal, phase)))
        elif phase == "DATA":
            for k in range(cal.get("data_chunks_common", 0)):
                out.append(("DATA", k, phase_duration(cal, "DATA", k)))
        else:
            for idx in sorted(ph[phase], key=int):
                out.append((phase, int(idx), ph[phase][idx]))
    return out


def evenly(points: list, m: int) -> list:
    if len(points) <= m:
        return list(points)
    if m <= 1:
        return points[:1]
    return [points[round(i * (len(points) - 1) / (m - 1))] for i in range(m)]


def sweep_points(cal: dict, step_us: int = 10, max_per_phase: int = 50) -> list[tuple[str, int, int]]:
    """Every phase x index x delay 0..duration (step_us), capped per phase at max_per_phase
    evenly spaced points. Deterministic: same calibration -> same list."""
    step_us = max(1, int(step_us))
    out = []
    targets = phase_targets(cal)
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


def double_second(rng: random.Random, cal: dict) -> tuple[str, int, int]:
    phase, idx = rng.choice(DOUBLE_SECOND)
    dur = phase_duration(cal, phase, idx) if phase != "META" else (
        phase_duration(cal, "META", idx) or phase_duration(cal, "META"))
    return phase, idx, rng.randint(0, max(0, dur))
