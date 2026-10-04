"""History review: compares a client's own history to itself and lists what
to improve (BUILD_SPEC §14). Read-only: produces findings, never proposals.

Every number is either computed from the client's data or read from
knowledge/*.json / engine-settings.json. No external benchmarks.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from config.loader import Config
from engine import data_quality
from engine.util import to_lb
from schemas.events import Event, SetLogged
from schemas.state import ClientState

Status = Literal["progressed", "flat", "regressed", "mixed", "insufficient_data"]
COLLECTED_STREAMS = {
    "weigh_in": "weigh-ins", "intake_logged": "food intake", "set_logged": "training sets",
    "soreness_rated": "soreness ratings", "stimulus_rated": "stimulus ratings (meso week 1)",
    "weekly_checkin": "weekly check-ins", "cardio_logged": "cardio",
}


# --- helpers -------------------------------------------------------------------

def best_set(sets: list[SetLogged]) -> SetLogged | None:
    """Heaviest set; ties broken by most reps."""
    return max(sets, key=lambda s: (s.load, s.reps)) if sets else None


def compare_sets(first: SetLogged | None, last: SetLogged | None) -> Status:
    """Direct load/rep comparison — no estimated 1RM."""
    if first is None or last is None:
        return "insufficient_data"
    if last.load > first.load:
        return "progressed" if last.reps >= first.reps else "mixed"
    if last.load < first.load:
        return "regressed" if last.reps <= first.reps else "mixed"
    if last.reps > first.reps:
        return "progressed"
    return "flat" if last.reps == first.reps else "regressed"


def _fmt(s: SetLogged | None) -> str | None:
    return f"{s.load:g} x {s.reps}" if s else None


class _Meso:
    def __init__(self, e: Event, end: date, as_of: date):
        self.meso_id: str = e.payload.meso_id
        self.start: date = e.day
        self.weeks: int = e.payload.weeks_planned
        self.end = end  # exclusive
        self.accum_end = min(end, self.start + timedelta(days=7 * self.weeks))  # exclusive
        self.complete = self.start + timedelta(days=7 * self.weeks) <= as_of + timedelta(days=1)

    def week(self, d: date) -> int:
        return (d - self.start).days // 7 + 1


def _mesos(events: list[Event], as_of: date) -> list[_Meso]:
    ms = sorted((e for e in events if e.type == "meso_started"), key=lambda e: e.day)
    out = []
    for i, e in enumerate(ms):
        end = ms[i + 1].day if i + 1 < len(ms) else as_of + timedelta(days=1)
        out.append(_Meso(e, end, as_of))
    return out


# --- training ------------------------------------------------------------------

def training_review(events: list[Event], state: ClientState, cfg: Config,
                    excluded: set[str]) -> dict[str, Any]:
    mesos = _mesos(events, state.as_of)
    sets = [e for e in events if e.type == "set_logged" and e.event_id not in excluded]
    exercises: dict[str, dict[str, Any]] = {}
    per_meso_rows: list[dict[str, Any]] = []
    for m in mesos:
        by_ex_week: dict[str, dict[int, list[SetLogged]]] = defaultdict(lambda: defaultdict(list))
        muscle_of: dict[str, str] = {}
        for e in sets:
            if m.start <= e.day < m.accum_end:
                by_ex_week[e.payload.exercise_id][m.week(e.day)].append(e.payload)
                muscle_of[e.payload.exercise_id] = e.payload.muscle
        for ex, weeks in sorted(by_ex_week.items()):
            first_wk, last_wk = min(weeks), max(weeks)
            first, last = best_set(weeks[first_wk]), best_set(weeks[last_wk])
            status = compare_sets(first, last) if last_wk > first_wk else "insufficient_data"
            per_meso_rows.append({
                "meso_id": m.meso_id, "complete": m.complete, "exercise": ex,
                "muscle": muscle_of[ex], "weeks": [first_wk, last_wk],
                "first_best": _fmt(first), "last_best": _fmt(last), "status": status})
            info = exercises.setdefault(ex, {"muscle": muscle_of[ex], "mesos": [], "best_by_week": []})
            info["mesos"].append(m.meso_id)
            info["best_by_week"].append((m.meso_id, {w: best_set(v) for w, v in weeks.items()}))

    # meso-to-meso: best set in the latest meso week both mesos have data for
    # (like-for-like, so an in-progress meso is not compared with a finished one's peak)
    meso_order = [m.meso_id for m in mesos]
    complete_ids = {m.meso_id for m in mesos if m.complete}
    across, rotation = [], []
    for ex, info in sorted(exercises.items()):
        bw = info["best_by_week"]
        for (pm, pw), (cm, cw) in zip(bw, bw[1:]):
            common = sorted(set(pw) & set(cw))
            wk = common[-1] if common else None
            ps, cs = (pw[wk], cw[wk]) if wk else (None, None)
            across.append({"exercise": ex, "from_meso": pm, "to_meso": cm, "meso_week": wk,
                           "from_best": _fmt(ps), "to_best": _fmt(cs),
                           "status": compare_sets(ps, cs)})
        # rotation candidate (training-defaults `exercise_selection.rotate`: staleness +
        # performance): kept across consecutive complete mesos and not progressing in the
        # latest complete one. An in-progress meso is too short to call a stall.
        done = [x for x in info["mesos"] if x in complete_ids]
        run = 1 if done else 0
        idx = [meso_order.index(x) for x in done]
        for a, b in zip(idx, idx[1:]):
            run = run + 1 if b == a + 1 else 1
        latest = next((r for r in reversed(per_meso_rows) if r["exercise"] == ex
                       and r["complete"] and r["status"] != "insufficient_data"), None)
        if run >= 2 and latest and latest["status"] in ("flat", "regressed"):
            rotation.append({"exercise": ex, "muscle": info["muscle"],
                             "consecutive_mesos": run, "latest_meso": latest["meso_id"],
                             "latest_status": latest["status"]})

    # volume per muscle per meso + where performance first fell to 3 (observed MRV hint)
    volume = []
    by_id = {m.meso_id: m for m in mesos}
    for muscle, weeks in sorted(state.muscle_weeks.items()):
        for meso_id in [m.meso_id for m in mesos]:
            mw = [w for w in weeks if w.meso_id == meso_id and w.meso_week <= by_id[meso_id].weeks]
            if not mw:
                continue
            drop = next((w for w in mw if w.performance == 3), None)
            volume.append({
                "muscle": muscle, "meso_id": meso_id,
                "weeks": [{"week": w.meso_week, "sets": w.sets, "performance": w.performance}
                          for w in mw],
                "performance_dropped": ({"week": drop.meso_week, "sets": drop.sets}
                                        if drop else None)})

    pain: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.type == "joint_pain_reported":
            p = pain.setdefault(e.payload.joint, {"reports": 0, "max_severity": 0, "exercises": set()})
            p["reports"] += 1
            p["max_severity"] = max(p["max_severity"], e.payload.severity)
            if e.payload.exercise_id:
                p["exercises"].add(e.payload.exercise_id)
    return {
        "mesos": [{"meso_id": m.meso_id, "start_date": m.start, "weeks_planned": m.weeks,
                   "complete": m.complete} for m in mesos],
        "exercise_progress_within_meso": per_meso_rows,
        "exercise_progress_across_mesos": across,
        "rotation_candidates": rotation,
        "volume_by_muscle": volume,
        "joint_pain": {j: {**v, "exercises": sorted(v["exercises"])} for j, v in sorted(pain.items())},
    }


# --- bodyweight & nutrition ---------------------------------------------------------

def phase_review(events: list[Event], state: ClientState, cfg: Config,
                 excluded: set[str]) -> list[dict[str, Any]]:
    as_of = state.as_of
    phases = sorted((e for e in events if e.type == "phase_started"), key=lambda e: e.day)
    min_n = cfg.nutrition("tracking.weigh_ins_per_week")[0]
    min_weeks = cfg.nutrition("tracking.assess_window_weeks")[1]
    kcal_per_lb = cfg.nutrition("tracking.kcal_per_lb_tissue")
    weigh = [e for e in events if e.type == "weigh_in" and e.event_id not in excluded]
    intake = [e for e in events if e.type == "intake_logged"]
    checkins = [e for e in events if e.type == "weekly_checkin"]
    out = []
    for i, ph in enumerate(phases):
        start = ph.day
        end = phases[i + 1].day if i + 1 < len(phases) else as_of + timedelta(days=1)  # exclusive
        n_weeks = (end - start).days // 7  # complete phase weeks only
        wk_vals: dict[int, list[float]] = defaultdict(list)
        for e in weigh:
            if start <= e.day < start + timedelta(days=7 * n_weeks):
                wk_vals[(e.day - start).days // 7 + 1].append(to_lb(e.payload.weight, e.payload.unit))
        weekly = {w: sum(v) / len(v) for w, v in wk_vals.items() if len(v) >= min_n}
        rates = []
        for w in sorted(weekly):
            if w - 1 in weekly:
                rates.append({"week": w, "pct_bw": round((weekly[w] - weekly[w - 1]) / weekly[w - 1] * 100, 3)})
        row: dict[str, Any] = {
            "phase": ph.payload.phase, "start_date": start, "end_date": end - timedelta(days=1),
            "complete_weeks": n_weeks, "target_rate_pct_bw": ph.payload.target_rate_pct_bw,
            "weeks_with_enough_weigh_ins": len(weekly), "weekly_rate_pct_bw": rates}
        band = None
        if ph.payload.phase in ("cut", "gain"):
            band = cfg.nutrition(f"phases.{ph.payload.phase}.rate_pct_bw_per_week")
            row["rate_band_pct_bw"] = band
        avg_change_lb = None
        if len(weekly) >= 2:
            first_w, last_w = min(weekly), max(weekly)
            avg_change_lb = (weekly[last_w] - weekly[first_w]) / (last_w - first_w)
            avg_rate = avg_change_lb / weekly[first_w] * 100
            row.update(start_avg_lb=round(weekly[first_w], 1), end_avg_lb=round(weekly[last_w], 1),
                       avg_rate_pct_bw=round(avg_rate, 3))
            if band:
                directional = -avg_rate if ph.payload.phase == "cut" else avg_rate
                row["rate_vs_band"] = ("below" if directional < band[0]
                                       else "above" if directional > band[1] else "within")
                row["weeks_in_band"] = sum(
                    1 for r in rates
                    if band[0] <= (-r["pct_bw"] if ph.payload.phase == "cut" else r["pct_bw"]) <= band[1])
        days_in = [e for e in intake if start <= e.payload.date < end]
        if days_in:
            avg_kcal = sum(e.payload.calories for e in days_in) / len(days_in)
            row["intake"] = {"days_logged": len(days_in), "days_in_phase": (end - start).days,
                             "avg_kcal": round(avg_kcal),
                             "avg_protein_g": round(sum(e.payload.protein_g for e in days_in) / len(days_in))}
            if avg_change_lb is not None and len(weekly) >= min_weeks:
                row["estimated_maintenance_kcal"] = round(avg_kcal - avg_change_lb * kcal_per_lb / 7)
        ck = [e.payload for e in checkins if start <= e.day < end]
        if ck:
            row["checkins"] = {
                "count": len(ck),
                "avg_adherence_pct": round(sum(c.adherence_pct for c in ck) / len(ck), 1),
                "hunger_first_last": [ck[0].hunger, ck[-1].hunger],
                "energy_first_last": [ck[0].energy, ck[-1].energy],
                "sleep_first_last": [ck[0].sleep, ck[-1].sleep]}
        out.append(row)
    return out


# --- macro target changes -------------------------------------------------------------

def _window_avgs(weigh: list[tuple[date, float]], anchor: date, n: int, after: bool,
                 min_n: int) -> list[float]:
    """Contiguous 7-day window averages moving away from `anchor` (after: anchor onward;
    before: the days just before anchor). Stops at the first window without enough weigh-ins."""
    out = []
    for k in range(n):
        lo = anchor + timedelta(days=7 * k) if after else anchor - timedelta(days=7 * (k + 1))
        vals = [w for d, w in weigh if lo <= d < lo + timedelta(days=7)]
        if len(vals) < min_n:
            break
        out.append(sum(vals) / len(vals))
    return out


def _rate(avgs: list[float], chronological: bool) -> float | None:
    if len(avgs) < 2:
        return None
    seq = avgs if chronological else list(reversed(avgs))
    return round((seq[-1] - seq[0]) / (len(seq) - 1), 2)


def macro_changes(events: list[Event], state: ClientState, cfg: Config,
                  excluded: set[str]) -> list[dict[str, Any]]:
    """Each change to prescribed macros, with the bodyweight trend in the weeks before and
    after it. Descriptive only: water shifts after a carb change are part of what it shows."""
    min_n = cfg.nutrition("tracking.weigh_ins_per_week")[0]
    n = cfg.nutrition("tracking.assess_window_weeks")[1]
    kcal = cfg.setting("energy.kcal_per_g")
    weigh = [(e.day, to_lb(e.payload.weight, e.payload.unit)) for e in events
             if e.type == "weigh_in" and e.event_id not in excluded]
    out = []
    for c in state.target_changes:
        if c.before is None:
            continue
        diff = {k: round(getattr(c.after, k) - getattr(c.before, k)) for k in ("protein_g", "carb_g", "fat_g")}
        before = _window_avgs(weigh, c.date, n, after=False, min_n=min_n)
        after = _window_avgs(weigh, c.date, n, after=True, min_n=min_n)
        out.append({
            "date": c.date, "day_type": c.day_type, "note": c.note,
            "before": c.before.model_dump(), "after": c.after.model_dump(), "change_g": diff,
            "kcal_change": round(diff["protein_g"] * kcal["protein"] + diff["carb_g"] * kcal["carb"]
                                 + diff["fat_g"] * kcal["fat"]),
            "lb_per_week_before": _rate(before, chronological=False),
            "weeks_before": len(before),
            "lb_per_week_after": _rate(after, chronological=True),
            "weeks_after": len(after),
        })
    return out


# --- data habits -------------------------------------------------------------------

def data_habits(events: list[Event], state: ClientState) -> dict[str, Any]:
    if not events:
        return {}
    first_day = min(e.day for e in events if e.type in COLLECTED_STREAMS) if any(
        e.type in COLLECTED_STREAMS for e in events) else state.as_of
    total_weeks = (state.as_of - first_day).days // 7 + 1
    streams = {}
    for etype, label in COLLECTED_STREAMS.items():
        days = sorted({e.day for e in events if e.type == etype})
        if not days:
            streams[etype] = {"label": label, "collected": False}
            continue
        weeks = {(state.as_of - d).days // 7 for d in days}
        streams[etype] = {"label": label, "collected": True, "first": days[0], "last": days[-1],
                          "weeks_with_data": len(weeks), "weeks_in_history": total_weeks}
    flag_counts: dict[str, int] = defaultdict(int)
    for f in state.data_quality:
        flag_counts[f.code] += 1
    return {"streams": streams, "flag_counts": dict(sorted(flag_counts.items()))}


# --- findings -------------------------------------------------------------------------

def findings(training: dict, phases: list[dict], habits: dict, cfg: Config,
             changes: list[dict] | None = None, limitations: list[dict] | None = None
             ) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(area: str, text: str) -> None:
        out.append({"area": area, "finding": text})

    for lim in limitations or []:
        add("limitations", f"{lim['area']}: {'; '.join(lim['restrictions'])}")
    for c in changes or []:
        moved = ", ".join(f"{k.removesuffix('_g')} {c['before'][k]:g}→{c['after'][k]:g}g"
                          for k in ("protein_g", "carb_g", "fat_g") if c["change_g"][k])
        trend = []
        for side in ("before", "after"):
            r = c[f"lb_per_week_{side}"]
            trend.append(f"{r:+.2f} lb/wk over {c[f'weeks_{side}']} wk {side}" if r is not None
                         else f"not enough weigh-ins {side}")
        add("nutrition", f"{c['date']} {c['day_type']} targets: {moved} ({c['kcal_change']:+d} kcal/day); "
                         f"weight trend {trend[0]} vs {trend[1]}")

    for p in phases:
        if p.get("rate_vs_band") in ("below", "above"):
            add("nutrition", f"{p['phase']} from {p['start_date']}: averaged {p['avg_rate_pct_bw']:+.2f}% BW/week "
                             f"vs target {p['target_rate_pct_bw']}% ({p['rate_vs_band']} the "
                             f"{p['rate_band_pct_bw'][0]}–{p['rate_band_pct_bw'][1]}% band); "
                             f"{p['weeks_in_band']}/{len(p['weekly_rate_pct_bw'])} weeks in band")
        if "estimated_maintenance_kcal" in p:
            add("nutrition", f"{p['phase']} from {p['start_date']}: intake {p['intake']['avg_kcal']} kcal/day "
                             f"with this weight change implies maintenance ≈ "
                             f"{p['estimated_maintenance_kcal']} kcal/day")
        ck = p.get("checkins")
        if ck and ck["avg_adherence_pct"] < cfg.setting("adherence_threshold_pct"):
            add("nutrition", f"{p['phase']} from {p['start_date']}: average adherence "
                             f"{ck['avg_adherence_pct']}% is below the {cfg.setting('adherence_threshold_pct')}% "
                             "threshold; results reflect adherence, not the plan")
        if ck and ck["hunger_first_last"][1] > ck["hunger_first_last"][0]:
            add("nutrition", f"{p['phase']} from {p['start_date']}: hunger rose "
                             f"{ck['hunger_first_last'][0]}→{ck['hunger_first_last'][1]}")

    complete = {m["meso_id"] for m in training["mesos"] if m["complete"]}
    if complete:
        last = [m["meso_id"] for m in training["mesos"] if m["complete"]][-1]
        rows = [r for r in training["exercise_progress_within_meso"] if r["meso_id"] == last]
        good = [r["exercise"] for r in rows if r["status"] == "progressed"]
        bad = [f"{r['exercise']} ({r['first_best']} → {r['last_best']})" for r in rows
               if r["status"] in ("flat", "regressed")]
        add("training", f"last complete meso {last}: {len(good)}/{len(rows)} exercises progressed")
        if bad:
            add("training", f"last complete meso {last}: no progress on " + ", ".join(bad))
    for r in training["exercise_progress_across_mesos"]:
        if r["status"] == "regressed" and r["to_meso"] in complete:
            add("training", f"{r['exercise']}: weaker in {r['to_meso']} than {r['from_meso']} at meso "
                            f"week {r['meso_week']} ({r['from_best']} → {r['to_best']})")
    for r in training["rotation_candidates"]:
        add("training", f"{r['exercise']} ({r['muscle']}): kept {r['consecutive_mesos']} mesos in a row and "
                        f"{r['latest_status']} in {r['latest_meso']}: rotation candidate")
    for v in training["volume_by_muscle"]:
        if v["performance_dropped"]:
            d = v["performance_dropped"]
            add("training", f"{v['muscle']}: couldn't match the previous week in {v['meso_id']} week "
                            f"{d['week']} at {d['sets']} sets/week (possible MRV)")
    for joint, p in training["joint_pain"].items():
        add("training", f"{joint}: {p['reports']} pain report(s), max severity {p['max_severity']}"
                        + (f" ({', '.join(p['exercises'])})" if p["exercises"] else ""))

    missing = [s["label"] for s in habits.get("streams", {}).values() if not s["collected"]]
    if missing:
        add("data", "never collected: " + ", ".join(missing))
    return out


def review(events: list[Event], state: ClientState, cfg: Config,
           excluded: set[str] | None = None) -> dict[str, Any]:
    events = sorted((e for e in events if e.day <= state.as_of),
                    key=lambda e: (e.timestamp, e.recorded_at, e.event_id))
    if excluded is None:
        excluded = data_quality.assess(events, state.as_of, cfg).excluded_event_ids
    training = training_review(events, state, cfg, excluded)
    phases = phase_review(events, state, cfg, excluded)
    habits = data_habits(events, state)
    changes = macro_changes(events, state, cfg, excluded)
    limitations = [lim.model_dump() for lim in state.limitations]
    return {"as_of": state.as_of, "training": training, "phases": phases,
            "macro_changes": changes, "limitations": limitations, "data_habits": habits,
            "findings": findings(training, phases, habits, cfg, changes, limitations)}
