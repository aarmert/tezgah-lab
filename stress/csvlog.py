"""trials.csv writer/reader (SPEC 9 column order) and the persistent wear counter."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .classify import normalize_outcome

COLUMNS = (
    "trial,seed,trial_seed,test,design,fault_type,phase,phase_index,delay_us,byte_offset,fault2_phase,"
    "fault2_index,fault2_delay_us,from_version,to_version,image,outcome,accepted,expect_ok,running_version,"
    "running_slot,slotA_sha256,slotB_sha256,slotA_match,slotB_match,meta_seq,meta_state,attempts,stlink_needed,"
    "resume_ok,resume_s,recovery_s,update_s,bytes,throughput_Bps,retries,fault_fired,err_code,meta_erases0,"
    "meta_erases1,slot_erases_A,slot_erases_B,trial_s,bl_line,app_line,note"
).split(",")


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v == v else ""
    return str(v)


class CsvLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        new = not self.path.exists()
        self.f = self.path.open("a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(COLUMNS)
            self.f.flush()

    def write(self, row: dict):
        unknown = set(row) - set(COLUMNS)
        if unknown:
            raise KeyError(f"unknown CSV columns: {sorted(unknown)}")
        self.w.writerow([fmt(row.get(c)) for c in COLUMNS])
        self.f.flush()

    def close(self):
        self.f.close()


def read_rows(path: Path) -> list[dict]:
    """Rows as dicts. Legacy (Turkish) outcome names are mapped to the current ones."""
    with Path(path).open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if "outcome" in r:
            r["outcome"] = normalize_outcome(r["outcome"])
    return rows


def note_kv(note: str) -> dict[str, str]:
    """'a=1; b=x; free text' -> {'a': '1', 'b': 'x'}."""
    out = {}
    for part in (note or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class Wear:
    """Host-side erase counter per board, persisted in <runs_dir>/wear.json (wear_sim.json for --sim)."""

    def __init__(self, path: Path, board_key: str):
        self.path = Path(path)
        self.key = board_key
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.cur = self.data.setdefault(board_key, {"slot_erases_A": 0, "slot_erases_B": 0,
                                                    "golden_flashes": 0, "trials": 0})

    def slot(self, s: str, n: int = 1):
        self.cur[f"slot_erases_{s}"] += n

    def golden(self):
        self.cur["golden_flashes"] += 1
        self.slot("A")
        self.slot("B")

    def trial(self):
        self.cur["trials"] += 1

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
