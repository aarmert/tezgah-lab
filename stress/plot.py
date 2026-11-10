"""Optional PNG plots for a run folder (matplotlib only if installed)."""
from __future__ import annotations

from pathlib import Path

from .classify import OUTCOMES
from .csvlog import read_rows

COLORS = {"NEW": "#2a9d8f", "OLD": "#8ab17d", "PENDING": "#e9c46a", "UNCLEAR": "#9e9e9e",
          "WRONG": "#e76f51", "DEAD": "#6d2e46"}


def write_plots(run_dir: Path) -> dict:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {"written": [], "message": "matplotlib is not installed; no plots (pip install matplotlib)"}
    run_dir = Path(run_dir)
    rows = read_rows(run_dir / "trials.csv")
    written = []

    # outcomes per phase (stacked bars)
    phases = sorted({r.get("phase") or r.get("fault_type") or "none" for r in rows})
    fig, ax = plt.subplots(figsize=(max(5, 0.8 * len(phases) + 2), 4))
    bottom = [0] * len(phases)
    for o in OUTCOMES:
        vals = [sum(1 for r in rows if (r.get("phase") or r.get("fault_type") or "none") == p
                    and r["outcome"] == o) for p in phases]
        if any(vals):
            ax.bar(phases, vals, bottom=bottom, label=o, color=COLORS[o])
            bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("trials")
    ax.set_title("Outcome per fault phase")
    ax.legend(frameon=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    p = run_dir / "outcomes_by_phase.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(str(p))

    # delay vs phase, coloured by outcome (sweeps)
    pts = [r for r in rows if r.get("delay_us")]
    if pts:
        fig, ax = plt.subplots(figsize=(7, 4))
        ph_idx = {p: i for i, p in enumerate(phases)}
        for o in OUTCOMES:
            sel = [r for r in pts if r["outcome"] == o]
            if sel:
                ax.scatter([float(r["delay_us"]) for r in sel],
                           [ph_idx.get(r.get("phase") or "none", 0) for r in sel],
                           s=14, label=o, color=COLORS[o])
        ax.set_yticks(range(len(phases)), phases)
        ax.set_xscale("symlog", linthresh=100)
        ax.set_xlabel("fault delay after the fault point (us)")
        ax.legend(frameon=False, fontsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        fig.tight_layout()
        p = run_dir / "delay_vs_phase.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

    ups = [float(r["update_s"]) for r in rows if r.get("update_s")]
    if ups:
        fig, ax = plt.subplots(figsize=(5, 3.5))
        ax.hist(ups, bins=min(30, max(5, len(ups) // 3)), color="#264653")
        ax.set_xlabel("update_s")
        ax.set_ylabel("trials")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        fig.tight_layout()
        p = run_dir / "update_s.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))
    return {"written": written, "message": f"{len(written)} PNG files"}
