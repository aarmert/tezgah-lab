"""Outcome classification (SPEC 7). Pure functions, no I/O. The first matching rule wins:

1. WRONG    a BL jump to slot X whose expected-length hash matches neither old nor new image,
            or an app banner whose version disagrees with the hash of the slot it reports
2. DEAD     no `BOOT app` line and no `BL update|resume|nofw` in the window
3. PENDING  the window ends with the BL in update/resume/nofw; accepted only if the host's resume
            reaches NEW within resume_limit_s (resume_ok)
4. OLD      the running app is the old version and its slot holds the old image
5. NEW      the running app is the new version and its slot holds the new image
6. UNCLEAR  nothing above matched (e.g. the last app banner was followed by a reset and nothing
            booted again). Not accepted. (Added to SPEC 7 as the explicit fall-through.)

Over the whole trial (update, observe and resume windows) the boots are counted as well
(count_boots / check_boots, SPEC 7 "unexpected reset"): a BL status line that no known cause
explains turns an otherwise safe outcome into UNCLEAR (not accepted). WRONG/DEAD stay.

The outcome names were Turkish until 2026-11-10 (YANLIŞ, ÖLÜ, BEKLİYOR, ESKİ, YENİ, BELİRSİZ).
Older trials.csv files still carry them; normalize_outcome() maps them to the English names.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .proto import Banner, Event

WRONG, DEAD, PENDING, OLD, NEW, UNCLEAR = "WRONG", "DEAD", "PENDING", "OLD", "NEW", "UNCLEAR"
OUTCOMES = (WRONG, DEAD, PENDING, OLD, NEW, UNCLEAR)
UNSAFE = (WRONG, DEAD)
# Pre-2026-11-10 outcome names in trials.csv -> current names
LEGACY_OUTCOMES = {"YANLIŞ": WRONG, "ÖLÜ": DEAD, "BEKLİYOR": PENDING, "ESKİ": OLD, "YENİ": NEW, "BELİRSİZ": UNCLEAR}


def normalize_outcome(o: str) -> str:
    """Map a legacy (Turkish) outcome name to the current one; anything else passes through."""
    return LEGACY_OUTCOMES.get(o, o)

BOOT_FLOW = {"status", "jump", "rollback", "repair", "wait", "timeout", "banner", "hardfault"}


@dataclass
class Expect:
    design: str
    old_base: str             # "v1"
    new_base: str             # "v2", "v2crash", "bad_crc", ...
    old_version: str          # "1.0.0"
    new_version: str          # "2.0.0-crash"
    versions: dict[str, str] = field(default_factory=dict)   # image base -> banner version
    new_runs: bool = True     # False for v2crash/v2hang/v2noconfirm: the update ends rolled back (OLD)

    @property
    def resume_goal(self) -> str:
        """What a finished resume must reach: NEW, or OLD (rolled back) for a failing new image."""
        return NEW if self.new_runs else OLD


@dataclass
class Window:
    """What the serial lines of one observe window say."""
    jumps: list[tuple[str, bool]]
    banners: list[Banner]
    running: Banner | None        # last banner, if no reset/fault followed it
    wait: str | None              # 'update' | 'resume' | 'nofw' if the window ends in it
    saw_wait: bool
    trial_jumps: int
    rollbacks: int
    fired: list[tuple]
    confirmed: bool               # 'APP confirmed' after the running banner
    last_status: str
    last_banner: str


def summarize(events: list[Event]) -> Window:
    jumps, banners, fired = [], [], []
    running, wait, saw_wait, confirmed = None, None, False, False
    rollbacks = 0
    last_status = last_banner = ""
    for e in events:
        if e.kind in BOOT_FLOW:
            wait = None
        if e.kind == "status":
            running, confirmed, last_status = None, False, e.data.line
        elif e.kind == "hardfault":
            running, confirmed = None, False
        elif e.kind == "jump":
            jumps.append(e.data)
        elif e.kind == "rollback":
            rollbacks += 1
        elif e.kind == "wait":
            wait, saw_wait = e.data, True
        elif e.kind == "banner":
            banners.append(e.data)
            running, confirmed, last_banner = e.data, False, e.data.line
        elif e.kind == "confirmed" and running is not None:
            confirmed = True
        elif e.kind == "fired":
            fired.append(e.data)
    return Window(jumps, banners, running, wait, saw_wait, sum(1 for _, t in jumps if t), rollbacks,
                  fired, confirmed, last_status, last_banner)


@dataclass
class Verdict:
    outcome: str
    accepted: bool
    reasons: list[str]
    running: Banner | None = None
    resume_ok: bool | None = None


def classify(events: list[Event], labels: dict[str, str], ex: Expect,
             resume_ok: bool | None = None) -> Verdict:
    """labels: slot -> match label from the ST-Link read taken right after the window."""
    w = summarize(events)
    known = {ex.old_base, ex.new_base}

    # 1. WRONG
    bad = []
    for slot, _trial in w.jumps:
        if labels.get(slot) not in known:
            bad.append(f"BL jumped to {slot}, but slot {slot} holds '{labels.get(slot)}' "
                       f"(neither {ex.old_base} nor {ex.new_base})")
    for b in w.banners:
        lab = labels.get(b.slot)
        want = ex.versions.get(lab)
        if want != b.full_version:
            bad.append(f"app banner says {b.full_version} slot={b.slot}, but slot {b.slot} holds '{lab}'"
                       + (f" ({want})" if want else ""))
    if bad:
        return Verdict(WRONG, False, bad, w.running)

    # 2. DEAD
    if not w.banners and not w.saw_wait:
        return Verdict(DEAD, False, ["no 'BOOT app' and no 'BL update|resume|nofw' in the observe window"])

    # 3. PENDING
    if w.wait is not None:
        ok = bool(resume_ok)
        why = {None: "not resumed", True: "resume reached NEW in time",
               False: "resume did not reach NEW in time"}[resume_ok]
        return Verdict(PENDING, ok, [f"BL is in '{w.wait}'; {why}"], None, resume_ok)

    r = w.running
    if r is not None:
        lab = labels.get(r.slot)
        # 4. OLD
        if r.full_version == ex.old_version and lab == ex.old_base:
            return Verdict(OLD, True, [f"old {r.full_version} runs from {r.slot}, slot holds {lab}"], r)
        # 5. NEW
        if r.full_version == ex.new_version and lab == ex.new_base:
            return Verdict(NEW, True, [f"new {r.full_version} runs from {r.slot}, slot holds {lab}"], r)
        return Verdict(UNCLEAR, False, [f"running {r.full_version} slot={r.slot} ('{lab}') is neither "
                                         f"old {ex.old_version} nor new {ex.new_version}"], r)
    return Verdict(UNCLEAR, False, ["the last app banner was followed by a reset and nothing booted again"])


def combine_resume(first: Verdict, resumed: Verdict | None, within_limit: bool, goal: str = NEW) -> Verdict:
    """The trial ended PENDING and the host resumed. A WRONG/DEAD during the resume wins
    (rule 1/2 precede rule 3 over the whole window); otherwise PENDING, accepted iff the goal
    (NEW; OLD after the rollback of a failing new image) was reached in time."""
    assert first.outcome == PENDING
    if resumed is not None and resumed.outcome in UNSAFE:
        return Verdict(resumed.outcome, False, first.reasons + ["during resume: " + "; ".join(resumed.reasons)],
                       resumed.running, False)
    ok = resumed is not None and resumed.outcome == goal and within_limit
    why = f"resume reached {goal} in time" if ok else (
        "resume timed out" if resumed is None else
        f"resume ended {resumed.outcome}" + ("" if within_limit else " (after resume_limit_s)"))
    reasons = [first.reasons[0].split(";")[0] + "; " + why]
    return Verdict(PENDING, ok, reasons, resumed.running if resumed else None, ok)


# --- boot accounting (SPEC 7, unexpected reset) ---------------------------------------------
EXPECTED_RESET_REPLIES = ("OK UPD", "OK COMMIT", "OK RESET")


@dataclass
class BootCount:
    boots: int            # BL status lines
    explained: int        # resets with a known cause
    causes: dict

    @property
    def unexpected(self) -> int:
        return max(0, self.boots - self.explained)


def count_boots(events: list[Event], external_resets: int = 0) -> BootCount:
    """Every reset makes the BL print one status line (unless the next reset comes first, which
    only lowers `boots`). Known causes: the app's OK UPD / OK RESET and the BL's OK COMMIT replies,
    an app HARDFAULT line (it resets), the faults that actually expired ('BL fault_fired n', n
    resets), and IWDG resets (IWDG in the status line's reset= flags). external_resets: NRST pulses
    the host fired itself (the ESP32 bridge's `ESP NRST FIRED`, --reset-source esp)."""
    causes = {"commands": 0, "hardfault": 0, "fired": 0, "iwdg": 0}
    if external_resets:
        causes["nrst"] = int(external_resets)
    boots = 0
    for e in events:
        if e.kind == "status":
            boots += 1
            if "IWDG" in e.data.reset.split(","):
                causes["iwdg"] += 1
        elif e.kind == "fired":
            causes["fired"] += e.data[0]
        elif e.kind == "hardfault":
            causes["hardfault"] += 1
        elif e.kind == "reply" and e.line.strip() in EXPECTED_RESET_REPLIES:
            causes["commands"] += 1
    return BootCount(boots, sum(causes.values()), causes)


def check_boots(v: Verdict, bc: BootCount) -> Verdict:
    """A trial with more boots than explained resets is not accepted: UNCLEAR, unless the outcome
    is already unsafe (WRONG/DEAD)."""
    n = bc.unexpected
    if not n or v.outcome in UNSAFE:
        return v
    why = (f"{n} unexpected reset(s): {bc.boots} BL boots, {bc.explained} explained "
           f"({', '.join(f'{k}={c}' for k, c in bc.causes.items() if c) or 'none'})")
    return Verdict(UNCLEAR, False, v.reasons + [why], v.running, v.resume_ok)
