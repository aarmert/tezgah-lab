"""Imports the repo's bench.py (never modified by this package)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bench  # noqa: E402,F401

BenchError = bench.BenchError
