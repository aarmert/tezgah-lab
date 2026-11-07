"""stress.toml (gitignored; stress.example.toml is committed). bench.toml stays bench.py's file."""
from __future__ import annotations

import copy
import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ._bench import ROOT, BenchError

DEFAULTS = {
    "stress": {
        "salt": "",
        "images_dir": "firmware/boot/build/images",
        "runs_dir": "stress_runs",
        "observe_s": 15.0,
        "resume_limit_s": 30.0,
        "settle_s": 1.0,
        "line_noise_rate": 0.05,
        "max_retries": 8,
    },
    # per-test observe window overrides (seconds); rollback needs 3 x (5 s deadline + 2 s IWDG)
    "observe_s": {"rollback": 45.0},
    "golden": {"ab": "golden_ab.bin", "naive": "golden_naive.bin"},
    # SPEC-C031C6 10: the C031's own image folder and bench config (bench.toml stays the F407's)
    "c031": {"images_dir": "firmware/boot_c0/build/images", "bench_config": "bench-c031.toml"},
    # SPEC-ESP32: the ESP32-S3 bridge on its CH343 UART0 port
    "esp": {"port": "", "baud": 921600, "nrst_pulse_us": 1000},
}


@dataclass
class StressConfig:
    path: Path | None
    data: dict

    def get(self, section: str, key: str, default=None):
        sec = self.data.get(section, {})
        if key in sec:
            return sec[key]
        return DEFAULTS.get(section, {}).get(key, default)

    def resolve(self, p) -> Path:
        p = Path(os.path.expanduser(str(p)))
        return p if p.is_absolute() else ROOT / p

    @property
    def runs_dir(self) -> Path:
        d = self.resolve(self.get("stress", "runs_dir"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def images_dir(self) -> Path:
        return self.resolve(self.get("stress", "images_dir"))

    def images_dir_for(self, target: str) -> Path:
        """f407: [stress] images_dir; c031: [c031] images_dir (default firmware/boot_c0/build/images)."""
        if target == "f407":
            return self.images_dir
        return self.resolve(self.get(target, "images_dir"))

    def bench_config_for(self, target: str, arg: str | None) -> str | None:
        """The bench.toml path for this target. --config wins. The C031 never falls back to
        bench.toml (the F407 bench): [c031] bench_config, default bench-c031.toml at the repo root."""
        if arg or target == "f407":
            return arg
        p = self.resolve(self.get(target, "bench_config"))
        if not p.is_file():
            raise BenchError("config_not_found", f"{p} not found (the {target} bench config).",
                             ["Copy bench-c031.example.toml to bench-c031.toml and set the ST-LINK VCP port",
                              "Or pass --config <file>"])
        return str(p)

    def golden_name(self, design: str) -> str:
        return self.get("golden", design, f"golden_{design}.bin")

    def observe_s(self, test: str) -> float:
        o = self.data.get("observe_s", {}).get(test, DEFAULTS["observe_s"].get(test))
        return float(o if o is not None else self.get("stress", "observe_s"))

    @property
    def salt(self) -> str:
        return str(self.get("stress", "salt") or "")

    def hash_id(self, value: str | None) -> str | None:
        if not value:
            return None
        return hashlib.sha256((self.salt + value).encode()).hexdigest()[:12]


def load_stress_config(arg: str | None = None, overrides: dict | None = None) -> StressConfig:
    cands = [Path(arg)] if arg else []
    if os.environ.get("STRESS_CONFIG"):
        cands.append(Path(os.environ["STRESS_CONFIG"]))
    cands.append(ROOT / "stress.toml")
    cfg = StressConfig(None, {})
    for c in cands:
        c = Path(os.path.expanduser(str(c)))
        if c.is_file():
            try:
                cfg = StressConfig(c.resolve(), tomllib.loads(c.read_text(encoding="utf-8")))
            except tomllib.TOMLDecodeError as e:
                raise BenchError("config_invalid", f"{c} could not be read: {e}", ["TOML syntax error"])
            break
        if arg and c == Path(arg):
            raise BenchError("config_not_found", f"{c} not found.", ["Copy stress.example.toml to stress.toml"])
    if overrides:
        cfg.data = copy.deepcopy(cfg.data)
        for sec, kv in overrides.items():
            cfg.data.setdefault(sec, {}).update(kv)
    return cfg
