"""summary.json / summary.md from trials.csv + run.json (SPEC 9), and the statistics.

Failure-rate bound: with k failures in n trials,
  k = 0: exact one-sided 95% upper bound 1 - 0.05^(1/n), shown with the rule of three 3/n
  k > 0: Clopper-Pearson one-sided 95% upper bound Beta^-1(0.95; k+1, n-k)
         (for k = 0 this formula gives exactly 1 - 0.05^(1/n)), plus the two-sided 95% CP interval.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .classify import OUTCOMES, UNSAFE
from .csvlog import note_kv, read_rows
from .tests_defs import verdict


# --- statistics -----------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def beta_ppf(p: float, a: float, b: float) -> float:
    """Inverse of I_x(a, b) by bisection (monotone in x)."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-15:
            break
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    a = 1 - conf
    lo = 0.0 if k == 0 else beta_ppf(a / 2, k, n - k + 1)
    hi = 1.0 if k == n else beta_ppf(1 - a / 2, k + 1, n - k)
    return lo, hi


def upper_bound(k: int, n: int, conf: float = 0.95) -> float:
    """One-sided upper confidence bound for the failure probability."""
    if n == 0:
        return 1.0
    if k == 0:
        return 1 - (1 - conf) ** (1 / n)
    if k >= n:
        return 1.0
    return beta_ppf(conf, k + 1, n - k)


def failure_stats(k: int, n: int) -> dict:
    d = {"failures": k, "n": n, "rate": (k / n) if n else None,
         "upper95": upper_bound(k, n) if n else None}
    if n:
        if k == 0:
            d["method"] = "exact zero-failure bound 1-0.05^(1/N)"
            d["rule_of_three"] = 3 / n
        else:
            d["method"] = "Clopper-Pearson one-sided 95%"
        d["cp95_two_sided"] = list(clopper_pearson(k, n))
    return d


def percentile(xs: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not xs:
        return None
    s = sorted(xs)
    pos = q * (len(s) - 1)
    i = int(math.floor(pos))
    j = min(i + 1, len(s) - 1)
    return s[i] + (s[j] - s[i]) * (pos - i)


def dist(xs: list[float]) -> dict:
    return {"n": len(xs), "median": percentile(xs, 0.5), "p95": percentile(xs, 0.95),
            "max": max(xs) if xs else None}


# --- summary ----------------------------------------------------------------------------
def _num(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize_rows(rows: list[dict], run: dict) -> dict:
    test = run.get("test") or (rows[0]["test"] if rows else "?")
    n = len(rows)
    counts = {o: 0 for o in OUTCOMES}
    phase_tab: dict[str, dict[str, int]] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        key = r.get("phase") or r.get("fault_type") or "none"
        if r.get("fault2_phase"):
            key = f"{key}+{r['fault2_phase']}"
        phase_tab.setdefault(key, {})
        phase_tab[key][r["outcome"]] = phase_tab[key].get(r["outcome"], 0) + 1
    expect_fail = sum(1 for r in rows if r.get("expect_ok") != "1")
    unsafe = sum(counts.get(o, 0) for o in UNSAFE)
    not_accepted = sum(1 for r in rows if r.get("accepted") != "1")
    col = lambda c: [x for x in (_num(r.get(c)) for r in rows) if x is not None]  # noqa: E731
    rb = [x for x in (_num(note_kv(r.get("note", "")).get("rollback_s")) for r in rows) if x is not None]
    ok, why = verdict(test, rows)
    last = rows[-1] if rows else {}
    wear = {k: last.get(k) for k in ("slot_erases_A", "slot_erases_B", "meta_erases0", "meta_erases1")}
    return {
        "test": test, "design": run.get("design"), "n": n, "seed": run.get("seed"),
        "sim": run.get("sim"), "interrupted": run.get("interrupted", False),
        "verdict": {"ok": ok, "summary": why},
        "counts": counts,
        "accepted": n - not_accepted,
        "failure": failure_stats(expect_fail, n),
        "unsafe": failure_stats(unsafe, n),
        "not_accepted": not_accepted,
        "per_phase": phase_tab,
        "update_s": dist(col("update_s")),
        "recovery_s": dist(col("recovery_s")),
        "resume_s": dist(col("resume_s")),
        "rollback_s": dist(rb),
        "throughput_Bps": dist(col("throughput_Bps")),
        "retries_total": int(sum(col("retries"))),
        "fault_fired_trials": sum(1 for x in col("fault_fired") if x > 0),
        "stlink_needed": sum(1 for r in rows if r.get("stlink_needed") == "1"),
        "wear": wear,
    }


def _f(x, nd=3, pct=False) -> str:
    if x is None:
        return "-"
    return f"{100 * x:.{nd}f}%" if pct else f"{x:.{nd}f}"


def render_md(s: dict, run: dict) -> str:
    v = s["verdict"]
    L = [f"# Stress test `{s['test']}` ({s['design']}{', sim' if s['sim'] else ''})", "",
         f"**{'PASS' if v['ok'] else 'FAIL'}**: {v['summary']}", ""]
    if s.get("interrupted"):
        L += ["> Interrupted (Ctrl-C): partial results.", ""]
    git = run.get("git", {})
    L += [f"- trials: {s['n']}, seed: {s['seed']}, commit: `{git.get('commit', '?')}`"
          f"{' (dirty)' if git.get('dirty') else ''}",
          f"- firmware manifest sha256: `{(run.get('firmware') or {}).get('manifest_sha256')}`",
          f"- reproduce: `{run.get('reproduce', '')}`", "",
          "## Outcomes", "", "| outcome | count |", "|---|---|"]
    for o, c in s["counts"].items():
        if c:
            L.append(f"| {o} | {c} |")
    f, u = s["failure"], s["unsafe"]
    L += ["", "## Failure rate", "",
          "| measure | k / n | rate | 95% upper bound | method |", "|---|---|---|---|---|"]
    for name, d in (("pass criterion failed", f), ("unsafe (DEAD+WRONG)", u)):
        extra = f"; 3/N = {_f(d.get('rule_of_three'), 2, True)}" if d.get("rule_of_three") is not None else ""
        cp = d.get("cp95_two_sided")
        extra += f"; two-sided CP95 [{_f(cp[0], 2, True)}, {_f(cp[1], 2, True)}]" if cp and d["failures"] else ""
        L.append(f"| {name} | {d['failures']} / {d['n']} | {_f(d['rate'], 2, True)} | "
                 f"{_f(d['upper95'], 2, True)} | {d.get('method', '-')}{extra} |")
    outs = [o for o in OUTCOMES if any(o in t for t in s["per_phase"].values())]
    L += ["", "## Phase x outcome", "", "| phase | " + " | ".join(outs) + " |",
          "|---|" + "---|" * len(outs)]
    for ph in sorted(s["per_phase"]):
        L.append(f"| {ph} | " + " | ".join(str(s["per_phase"][ph].get(o, 0)) for o in outs) + " |")
    L += ["", "## Timing (s)", "", "| measure | n | median | p95 | max |", "|---|---|---|---|---|"]
    for k in ("update_s", "recovery_s", "resume_s", "rollback_s", "throughput_Bps"):
        d = s[k]
        if d["n"]:
            L.append(f"| {k} | {d['n']} | {_f(d['median'])} | {_f(d['p95'])} | {_f(d['max'])} |")
    L += ["", f"- retries (total): {s['retries_total']}",
          f"- trials where a fault fired: {s['fault_fired_trials']}",
          f"- trials that needed the ST-Link (recovery): {s['stlink_needed']}", "",
          "## Wear (erase counts after the last trial)", "", "| counter | value |", "|---|---|"]
    for k, val in s["wear"].items():
        L.append(f"| {k} | {val if val not in (None, '') else '-'} |")
    L.append("")
    return "\n".join(L)


def write_summary(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    rows = read_rows(run_dir / "trials.csv") if (run_dir / "trials.csv").is_file() else []
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}
    s = summarize_rows(rows, run)
    (run_dir / "summary.json").write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "summary.md").write_text(render_md(s, run), encoding="utf-8")
    return s
