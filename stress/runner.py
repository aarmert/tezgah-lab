"""One trial = baseline -> enter update -> arm faults -> transfer -> observe -> ST-Link read ->
classify -> (resume) -> (smoke) -> (recovery). Same code for hardware and --sim."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from ._bench import BenchError
from .classify import (PENDING, UNCLEAR, OLD, DEAD, WRONG, NEW, Expect, Verdict, check_boots, classify,
                       combine_resume, count_boots, summarize)
from .config import StressConfig
from .csvlog import Wear
from .images import Image, ImageSet
from .layout import Layout, other_slot
from .meta import free_index
from .link import Link
from .proto import Event
from .stlink import FlashState, Probe
from .tests_defs import TrialPlan, judge
from .updater import EnterResult, Injector, TransferResult, Updater, arm_faults, enter_update

BOOT_WAIT_S = 8.0          # after a reset/golden flash: covers a 3 s 'BL resume' wait + boots
ENTER_TRIES = 3            # enter_update attempts before a trial counts as a setup failure


@dataclass
class Ctx:
    scfg: StressConfig
    images: ImageSet
    design: str
    link: Link
    probe: Probe
    wear: Wear
    test: str
    baseline: str = "any"
    observe_s: float = 15.0
    resume_limit_s: float = 30.0
    settle_s: float = 1.0
    max_retries: int = 8
    hooks: bool | None = None
    uid: str | None = None
    versions: dict = field(default_factory=dict)
    bridge: object = None            # EspBridgeTransport when --link esp (also the link's transport)
    witness: object = None           # esp_bridge.Witness on the VCP when --link esp
    reset_source: str = "hook"       # hook | esp (SPEC-ESP32 3)

    @property
    def clock(self):
        return self.link.clock

    @property
    def layout(self) -> Layout:
        return self.images.layout

    def golden(self):
        return self.images.dir / self.scfg.golden_name(self.design)


# --- observe -------------------------------------------------------------------------
def observe(link: Link, start: int, observe_s: float, settle_s: float) -> list[Event]:
    """Collect events from link.events[start:] until the board settles, the BL waits in
    update/resume/nofw, or observe_s passes. Settled = a confirmed app (trial=0 banner, or
    'APP confirmed' after a trial banner) with no reset for settle_s."""
    link.cursor = start
    clock = link.clock
    deadline = clock.now() + observe_s
    stable_since = None
    events = []
    while True:
        now = clock.now()
        if deadline - now <= 1e-6:
            return events
        wait = deadline - now
        if stable_since is not None:
            wait = min(wait, stable_since + settle_s - now)
            if wait <= 1e-6:
                return events
        e = link.next_event(wait)
        if e is None:
            continue
        events.append(e)
        if e.kind == "wait":
            return events
        if e.kind in ("status", "hardfault", "jump", "rollback", "repair", "timeout"):
            stable_since = None
        elif e.kind == "banner":
            stable_since = None if e.data.trial else clock.now()
        elif e.kind == "confirmed":
            stable_since = clock.now()


def wait_banner(link: Link, timeout: float) -> Event | None:
    e, _ = link.wait_for(lambda ev: ev.kind == "banner", timeout)
    return e


def query_version(link: Link, tries: int = 2) -> str | None:
    for _ in range(tries):
        e = link.command("VER", r"^VER (\S+)$", 1.0)
        if e is not None:
            return e.line.split()[1]
    return None


# --- baseline / recovery -------------------------------------------------------------
def baseline_state(ctx: Ctx, st: FlashState) -> tuple[Image | None, str]:
    strict = ctx.baseline == "strict"
    if ctx.design == "ab":
        rec = st.meta()
        if rec is None:
            return None, "no valid META record"
        if rec.trial_slot is not None:
            return None, f"a trial is pending (trial={rec.trial_slot})"
        slot = rec.active_slot
        if slot not in ("A", "B") or not st.valid(slot):
            return None, f"active slot {slot} is not valid"
    else:
        slot = "A"
    label = st.labels(ctx.images)[slot]
    if label not in ("v1", "v2"):
        return None, f"slot {slot} holds '{label}', not v1/v2"
    if strict and (slot != "A" or label != "v1"):
        return None, f"--baseline strict wants v1 in A, found {label} in {slot}"
    return ctx.images.get(label, slot), "ok"


def fault_clear(ctx: Ctx):
    """Clear leftover fault entries in noinit (an unfired entry 1 would hit the next boot)."""
    if ctx.hooks is False:
        return
    e = ctx.link.command("FAULT CLEAR", r"^(OK FAULT CLEAR|ERR)", 1.0)
    if e is not None and e.line.startswith("ERR"):
        ctx.hooks = False


@dataclass
class Recovery:
    ok: bool
    seconds: float
    actions: list[str]


def recover(ctx: Ctx, skip_reset: bool = False) -> Recovery:
    """Reset first; if the board does not come back to an acceptable baseline, flash
    golden_<design>.bin under reset and verify it over ST-Link."""
    link, t0, actions = ctx.link, ctx.clock.now(), []
    if not skip_reset:
        link.drain()
        link.note("recovery: reset")
        try:
            ctx.probe.reset()
            b = wait_banner(link, BOOT_WAIT_S)
            actions.append(f"reset: {'banner' if b else 'no banner'}")
            if b is not None:
                img, why = baseline_state(ctx, ctx.probe.read_state())
                if img is not None and img.header.version == b.data.version:
                    fault_clear(ctx)
                    return Recovery(True, ctx.clock.now() - t0, actions)
                actions.append(f"after reset: {why}")
        except BenchError as e:
            actions.append(f"reset failed: {e.summary}")
    link.drain()
    link.note(f"recovery: flash {ctx.golden().name} under reset")
    ctx.probe.flash_golden(ctx.golden())
    ctx.wear.golden()
    b = wait_banner(link, BOOT_WAIT_S)
    st = ctx.probe.read_state()
    img, why = baseline_state(ctx, st)
    ok = b is not None and img is not None
    actions.append(f"golden: banner={'yes' if b else 'no'}, state={why}")
    if ok:
        fault_clear(ctx)
    return Recovery(ok, ctx.clock.now() - t0, actions)


@dataclass
class Baseline:
    old: Image
    state: FlashState
    recovered: bool
    notes: list[str]


META_NEAR_FULL_FREE = 2        # metafull baseline: at most this many free META0 record slots


def flash_metafull(ctx: Ctx) -> None:
    """--meta-near-full: before every trial, write golden_ab_metafull's S1..S4 (META0 one record short
    of full, v1 in A, B and META1 erased) under reset. The BL sector is left alone."""
    g = ctx.images.golden.get("ab_metafull")
    if g is None:
        raise BenchError("images_missing", f"golden_ab_metafull.bin not found in {ctx.images.dir}.",
                         ["Rebuild: python3 firmware/boot/build_boot.py --json"])
    ctx.link.drain()
    ctx.link.note("baseline: flash golden_ab_metafull (S1..S4) under reset")
    ctx.probe.flash_golden(g, skip_bl=True)
    ctx.wear.golden()
    wait_banner(ctx.link, BOOT_WAIT_S)


def metafull_problem(st: FlashState) -> str | None:
    rec = st.meta()
    idx = free_index(st.META0)
    L = st.layout
    free = 0 if idx is None else L.records_per_meta() - idx
    if rec is None or rec.sector != L.meta_units[0] or idx is None or free > META_NEAR_FULL_FREE:
        return f"META0 is not near full (latest in sector {rec.sector if rec else '-'}, {free} free records)"
    return None


def ensure_baseline(ctx: Ctx, scenario: str = "normal") -> Baseline:
    notes, recovered = [], False
    if scenario == "metafull":
        flash_metafull(ctx)
        notes.append("baseline: golden_ab_metafull")
    for attempt in range(2):
        st = ctx.probe.read_state()
        old, why = baseline_state(ctx, st)
        if old is None:
            notes.append(f"baseline: {why}; flashing golden")
            r = recover(ctx, skip_reset=True)
            recovered = True
            notes += r.actions
            if not r.ok:
                raise BenchError("baseline_failed", f"Board not in a usable state after golden flash: {why}",
                                 ["Images do not match the firmware on the board (rebuild firmware/boot)",
                                  "Check the serial wiring (no BOOT banner seen)",
                                  "python bench.py --json halt-dump"])
            continue
        ver = query_version(ctx.link)
        if ver is None:
            ctx.link.note("baseline: no VER reply, resetting")
            ctx.probe.reset()
            wait_banner(ctx.link, BOOT_WAIT_S)
            ver = query_version(ctx.link)
        if ver != old.header.version:
            notes.append(f"baseline: app says {ver}, flash says {old.version}")
            r = recover(ctx, skip_reset=True)
            recovered = True
            notes += r.actions
            continue
        if scenario == "metafull" and (why := metafull_problem(st)):
            raise BenchError("baseline_failed", f"--meta-near-full precondition not met: {why}.",
                             ["Rebuild the images (golden_ab_metafull.bin)", "See serial.log"])
        fault_clear(ctx)
        return Baseline(old, st, recovered, notes)
    raise BenchError("baseline_failed", "Could not reach a clean baseline (confirmed v1/v2, app answering VER).",
                     ["See serial.log", "python bench.py --json halt-dump"])


# --- resume ----------------------------------------------------------------------------
def enter_update_retry(link: Link, noise: Injector | None = None, tries: int = ENTER_TRIES) -> EnterResult:
    """enter_update(); when the BL booted instead of printing 'BL update' (a jump or a banner, or
    nothing), wait for the app and try again. The last result is returned (ok or not)."""
    er, retries = None, 0
    for _ in range(tries):
        er = enter_update(link, noise)
        retries += er.retries
        if er.ok:
            break
        retries += 1
        link.note(f"enter update failed ({er.why}); waiting for the app and retrying")
        wait_banner(link, BOOT_WAIT_S)
    er.retries = retries
    return er


@dataclass
class ResumeResult:
    verdict: Verdict | None
    seconds: float
    events: list[Event]
    state: FlashState | None
    transfers: list[TransferResult]
    notes: list[str] = field(default_factory=list)
    reentered: int = 0             # times the BL had left update mode and the host re-entered via UPD


def do_resume(ctx: Ctx, old: Image, new: Image, ex: Expect) -> ResumeResult:
    """The BL waits in update/resume/nofw: finish the update and see whether NEW follows.
    If the BL has already left update mode (its 3 s wait ran out: 'BL timeout', then it booted the
    old app), the transfer does not start blindly: the window is classified, and an OLD app is
    sent 'UPD' to enter update mode again (recorded as resume_reentered)."""
    link, t0 = ctx.link, ctx.clock.now()
    deadline = t0 + ctx.resume_limit_s
    events, transfers, notes = [], [], []
    reentered = 0
    in_bl = True
    for _ in range(6):
        if ctx.clock.now() >= deadline:
            break
        if not in_bl:
            er = enter_update_retry(link)
            if not er.ok:
                notes.append(f"resume: could not enter update mode ({er.why})")
                break
            reentered += 1
        link.note("resume: transfer again")
        up = Updater(link, Injector(), ctx.max_retries).transfer(new)
        transfers.append(up)
        ctx.wear.slot(new.slot, up.erases)
        if up.status == "left":
            notes.append(f"resume: {up.notes[-1]}")
        start = up.reset_index if up.status in ("reset", "left") else link.cursor
        if up.status in ("err", "failed"):
            start = link.mark()
            link.command("UPD ABORT", r"^OK ABORT$", 1.0)
        ev = observe(link, start, ctx.observe_s, ctx.settle_s)
        events += ev
        w = summarize(ev)
        if w.wait is not None:
            in_bl = True
            continue
        st = ctx.probe.read_state()
        v = classify(ev, st.labels(ctx.images, [old, new]), ex)
        rolled_back = not ex.new_runs and up.status == "commit"
        if v.outcome == OLD and not rolled_back:   # BL gave up waiting and booted the old app: update it again
            in_bl = False
            continue
        return ResumeResult(v, ctx.clock.now() - t0, events, st, transfers, notes, reentered)
    return ResumeResult(None, ctx.clock.now() - t0, events, None, transfers, notes, reentered)


# --- trial -----------------------------------------------------------------------------
def fault2_state(plan: TrialPlan, fired: list[tuple]) -> str:
    """Did entry 1 fire? A report gives the count and the last fault only: two expiries mean both
    entries fired; one report matching entry 1 only means entry 1."""
    def match(entry, d):
        return entry is not None and d[1] == entry[0] and (entry[1] < 0 or d[2] == entry[1])
    total = sum(d[0] for d in fired)
    if total >= 2:
        return "fired"
    if total == 1:
        hit1 = any(match(plan.entry1, d) for d in fired)
        hit0 = any(match(plan.entry0, d) for d in fired)
        return "fired" if hit1 and not hit0 else ("ambiguous" if hit1 else "not_fired")
    return "not_fired"



def smoke(ctx: Ctx, new: Image) -> tuple[bool, str]:
    ver = re.escape(new.header.version)
    for cmd, rx in (("PING", r"^PONG$"), ("VER", rf"^VER {ver}$"), ("TICK", r"^TICK \d+$"),
                    ("UID", r"^UID [0-9A-F]{24}$")):
        e = ctx.link.command(cmd, rx, 1.0)
        if e is None:
            return False, f"{cmd}: no matching reply"
        if cmd == "UID" and ctx.uid is None:
            ctx.uid = e.line.split()[1]
    return True, ""


def run_trial(ctx: Ctx, plan: TrialPlan) -> tuple[dict, dict]:
    """Returns (csv row, extras for calibrate/tests)."""
    link, clock = ctx.link, ctx.clock
    t0 = clock.now()
    mark0 = link.mark()
    link.note(f"=== trial {plan.trial} test={plan.test} {plan.describe()}")
    notes: list[str] = []
    row = {"trial": plan.trial, "seed": plan.seed, "trial_seed": plan.trial_seed, "test": plan.test,
           "design": ctx.design, "fault_type": plan.fault_type, "stlink_needed": False}
    if plan.entry0:
        row["phase"], row["phase_index"], row["delay_us"] = plan.entry0
    if plan.entry1:
        row["fault2_phase"], row["fault2_index"], row["fault2_delay_us"] = plan.entry1

    base = ensure_baseline(ctx, plan.scenario)
    if plan.scenario != "normal":
        notes.append(f"scenario={plan.scenario}")
    if base.recovered:
        notes.append("baseline needed golden flash")
    old = base.old
    tgt = other_slot(old.slot) if ctx.design == "ab" else "A"
    new_base = plan.new_base or ("v2" if old.base == "v1" else "v1")
    new = ctx.images.get(new_base, tgt)
    ex = Expect(ctx.design, old.base, new.base, old.version, new.version, ctx.versions,
                new_runs=new.base in ("v1", "v2"))
    row.update(from_version=old.version, to_version=new.version, image=new.name)

    rng = random.Random(plan.trial_seed)
    noise = Injector(rng, plan.noise_rate) if plan.noise_rate else None
    entries = [plan.entry0, plan.entry1]
    cut_at = int(plan.cut_u * len(new.data)) if plan.cut_u is not None else None
    # With no armed fault and no cut, the host may start the whole update again when the BL gave
    # up (BL timeout after lost lines) or the line retries ran out. An unexpected reset is never
    # retried, so a restart cannot hide a fault.
    can_restart = not any(entries) and cut_at is None and not plan.trace
    inj = Injector(rng, plan.noise_rate, cut_at=cut_at)
    # --reset-source esp: entry 0 is an ESP32 NRST pulse armed on its trigger line, not FAULT ARM
    esp_entry = plan.entry0 if plan.reset_source == "esp" and plan.entry0 else None
    armer = None
    fires_mark = len(ctx.bridge.fires) if ctx.bridge is not None else 0
    wit_mark = ctx.witness.mark() if ctx.witness is not None else 0
    if esp_entry is not None:
        if ctx.bridge is None:
            raise BenchError("bad_argument", "--reset-source esp needs --link esp (the ESP32 bridge).")
        from .esp_bridge import EspArmer
        hook_entries = [None, plan.entry1]
    else:
        hook_entries = entries
    t_upd = clock.now()
    upd_mark = link.mark()          # boot accounting covers everything from the first UPD on
    retries, restarts = 0, 0
    up: TransferResult | None = None
    setup_failed = None
    while True:
        er = enter_update_retry(link, noise)
        retries += er.retries
        if er.status is not None:
            ctx.hooks = er.status.hooks
            if er.status.design != ctx.design:
                raise BenchError("wrong_firmware", f"BL reports design={er.status.design}, run wants {ctx.design}.",
                                 [f"Flash golden_{ctx.design}.bin or pass --design {er.status.design}"])
            if er.status.target != ctx.layout.target:
                raise BenchError("wrong_firmware", f"BL reports target={er.status.target}, run wants "
                                 f"--target {ctx.layout.target}.",
                                 [f"Pass --target {er.status.target}", "Check --config (bench-c031.toml for the C031)"])
        if not er.ok:
            # No 'BL update' after ENTER_TRIES attempts: nothing was updated, so there is nothing to
            # observe (an empty window would read as DEAD). The trial is a setup failure.
            setup_failed = f"could not enter update mode after {ENTER_TRIES} tries ({er.why})"
            break
        if any(hook_entries) or plan.trace:
            err = arm_faults(link, hook_entries, trace=plan.trace)
            if err:
                raise BenchError("hooks_missing", err,
                                 ["This test needs the hooks build of the BL/apps (BL status line shows 'hooks')"])
        if esp_entry is not None:
            armer = EspArmer(ctx.bridge, esp_entry, link)
        up = Updater(link, inj, ctx.max_retries, before_send=armer).transfer(new)
        retries += up.retries
        ctx.wear.slot(tgt, up.erases)
        start = up.reset_index if up.status in ("reset", "left") else link.cursor
        gave_up = up.status == "left" or (up.status == "reset" and up.left_kind in ("timeout", "jump", "banner"))
        if can_restart and restarts < 3 and (gave_up or up.status == "failed"):
            restarts += 1
            retries += 1
            notes.append(f"restart {restarts}: {up.notes[-1] if up.notes else up.err_line}")
            if up.status == "failed":
                link.command("UPD ABORT", r"^OK ABORT$", 1.0)
            if up.left_kind != "banner":
                wait_banner(link, BOOT_WAIT_S)
            continue
        break
    if up is not None:
        if cut_at is not None:
            row["byte_offset"] = cut_at
        if inj.noisy or (noise and noise.noisy):
            counts = {k: inj.counts[k] + (noise.counts[k] if noise else 0) for k in inj.counts}
            notes.append("noise " + ",".join(f"{k}={v}" for k, v in counts.items()))
        if up.status in ("err", "failed"):
            row["err_code"] = up.err_code
            notes.append(f"transfer {up.status}: {up.err_line}")
            start = link.mark()
            link.command("UPD ABORT", r"^OK ABORT$", 1.0)
        elif up.status == "cut":
            notes.append(f"host stopped at byte {cut_at}")
        elif up.status == "left":
            notes.append(f"transfer: {up.notes[-1]}")
        row["bytes"] = up.bytes
        if up.transfer_s:
            row["throughput_Bps"] = round(len(new.data) / up.transfer_s, 1)

    cands = [old, new]
    resume_info: ResumeResult | None = None
    if setup_failed:
        notes.append(f"setup_failed: {setup_failed}")
        events, w = [], summarize([])
        st = ctx.probe.read_state()
        labels = st.labels(ctx.images, cands)
        v = Verdict(UNCLEAR, False, [f"setup failed: {setup_failed}; no update was attempted"])
    else:
        events = observe(link, start, ctx.observe_s, ctx.settle_s)
        w = summarize(events)
        if w.wait is not None and not w.jumps and not w.banners:
            # SPEC 7 rule 3 with nothing for rule 1 to check: resume at once. The BL waits only 3 s
            # in update/resume, and the ST-Link read may take longer; it is done after the resume.
            resume_info = do_resume(ctx, old, new, ex)
        st = (resume_info.state if resume_info is not None and resume_info.state is not None
              else ctx.probe.read_state())
        labels = st.labels(ctx.images, cands)
        v = classify(events, labels, ex)
    all_events = list(events)

    if v.outcome == PENDING:
        rr = resume_info or do_resume(ctx, old, new, ex)
        all_events += rr.events
        within = rr.seconds <= ctx.resume_limit_s
        v = combine_resume(v, rr.verdict, within, ex.resume_goal)
        row["resume_ok"] = v.resume_ok
        row["resume_s"] = round(rr.seconds, 3)
        retries += sum(t.retries for t in rr.transfers)
        notes += rr.notes
        if rr.reentered:
            notes.append(f"resume_reentered={rr.reentered}")
        if rr.state is not None:
            st = rr.state
            labels = st.labels(ctx.images, cands)
        resume_info = rr
        if v.running is not None:
            w = summarize(rr.events)

    # ESP pulses: the host explains the resets it fired. An armed pulse that has not been reported
    # yet (its delay ran past the window) is waited for, then the bridge is disarmed.
    fires = []
    if ctx.bridge is not None and esp_entry is not None:
        if armer is not None and armer.armed:
            fires = ctx.bridge.settle_fires(fires_mark, 0.5 + esp_entry[2] / 1e6)
        try:
            ctx.bridge.disarm()
        except BenchError as e:
            notes.append(f"esp disarm failed: {e.summary}")
        fires = ctx.bridge.fires_since(fires_mark)
        notes.append("reset_source=esp")
        if not armer or not armer.armed:
            notes.append("esp_not_armed (trigger line never sent)")
        elif not fires:
            notes.append("esp_not_fired")
        for f in fires:
            notes.append(f"late_us={f.late_us}")

    # SPEC 7: boots the trial cannot explain (an unexpected reset) are never accepted
    bc = count_boots(link.events[upd_mark:], external_resets=len(fires))
    if bc.unexpected:
        notes.append(f"unexpected_reset={bc.unexpected}")
        v = check_boots(v, bc)

    facts = {"outcome": v.outcome, "accepted": v.accepted, "err_code": row.get("err_code"),
             "expect_err": plan.expect_err, "trial_jumps": w.trial_jumps, "rollbacks": w.rollbacks}
    facts["active_unchanged"] = st.slot_sha(old.slot) == base.state.slot_sha(old.slot)
    if v.outcome == NEW and plan.test == "basic":
        ok, why = smoke(ctx, new)
        facts["smoke_ok"] = ok
        if not ok:
            notes.append(f"smoke: {why}")
    expect_ok, why = judge(plan.test, facts)
    if why:
        notes.append(why)

    # timings
    final = v.running
    if v.outcome == NEW and final is not None:
        t_ok = next((e.t for e in reversed(all_events) if e.kind in ("confirmed", "banner")), None)
        if t_ok is not None:
            row["update_s"] = round(t_ok - t_upd, 3)
    if up is not None and up.t_commit is not None and v.outcome == OLD and w.rollbacks:
        t_old = next((e.t for e in reversed(events) if e.kind == "banner"), None)
        if t_old is not None:
            notes.append(f"rollback_s={t_old - up.t_commit:.3f}")
    if plan.test == "rollback":
        notes.append(f"trial_jumps={w.trial_jumps}")

    trial_events = link.events[mark0:]
    # recovery
    if v.outcome in (DEAD, WRONG, UNCLEAR) or (v.outcome == PENDING and not v.accepted):
        rec = recover(ctx)
        row["stlink_needed"] = True
        row["recovery_s"] = round(rec.seconds, 3)
        notes.append("recovery " + ("ok" if rec.ok else "FAILED") + ": " + " | ".join(rec.actions))
        if not rec.ok:
            raise BenchError("recovery_failed", f"Trial {plan.trial}: recovery failed; stopping the run.",
                             ["python bench.py --json halt-dump", "python bench.py --json recover"])
    elif plan.entry1 is not None or plan.trace:
        fault_clear(ctx)
        if plan.trace:
            ctx.link.command("FAULT TRACE OFF", r"^(OK FAULT TRACE|ERR)", 1.0)

    rec_meta = st.meta() if ctx.design == "ab" else None
    fired_ev = [e.data for e in link.events[upd_mark:] if e.kind == "fired"]
    fired = sum(d[0] for d in fired_ev) + len(fires)
    if plan.entry1 is not None:
        notes.append(f"fault2={fault2_state(plan, fired_ev)}")
    if ctx.witness is not None:
        ctx.witness.settle(0.2)
        from .esp_bridge import compare
        miss = compare([e.line for e in link.events[upd_mark:]], [ln for _, ln in ctx.witness.lines[wit_mark:]])
        if miss:
            notes.append(miss)
    ctx.wear.trial()
    ctx.wear.save()
    row.update(
        outcome=v.outcome, accepted=v.accepted, expect_ok=expect_ok,
        running_version=final.full_version if final else None, running_slot=final.slot if final else None,
        slotA_sha256=st.slot_sha("A"), slotB_sha256=st.slot_sha("B"),
        slotA_match=labels["A"], slotB_match=labels["B"],
        meta_seq=rec_meta.seq if rec_meta else None, meta_state=rec_meta.state if rec_meta else None,
        attempts=rec_meta.attempts if rec_meta else None,
        retries=retries, fault_fired=fired,
        meta_erases0=rec_meta.erases()[0] if rec_meta else None,
        meta_erases1=rec_meta.erases()[1] if rec_meta else None,
        slot_erases_A=ctx.wear.cur["slot_erases_A"], slot_erases_B=ctx.wear.cur["slot_erases_B"],
        trial_s=round(clock.now() - t0, 3),
        bl_line=w.last_status or None, app_line=w.last_banner or None,
        note="; ".join([*notes, *v.reasons] + base.notes),
    )
    link.flush_log()
    extras = {"verdict": v, "transfer": up, "events": trial_events, "state": st, "labels": labels,
              "resume": resume_info, "old": old, "new": new}
    return row, extras
