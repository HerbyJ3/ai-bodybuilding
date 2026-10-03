"""Data-quality flags (BUILD_SPEC §7.5). Flags, never fails.

Weekly checks use trailing 7-day windows ending at `as_of`, starting from the
first event of the stream. Consecutive windows with the same problem are
merged into one flag so a months-long import yields a readable gap report.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from config.loader import Config
from engine.util import to_lb, window_bounds, window_index
from schemas.events import Event
from schemas.state import DataQualityFlag

GAP_STREAMS = {
    "weigh_in": "weigh_in",
    "intake_logged": "intake",
    "set_logged": "sets",
    "weekly_checkin": "checkin",
    "soreness_rated": "soreness",
}


@dataclass(frozen=True)
class MesoSpan:
    meso_id: str
    start: date
    end: date  # exclusive
    weeks_planned: int


@dataclass
class DQResult:
    flags: list[DataQualityFlag] = field(default_factory=list)
    excluded_event_ids: set[str] = field(default_factory=set)

    def codes(self) -> list[str]:
        return sorted({f.code for f in self.flags})


def _merge_windows(code: str, stream: str, windows: list[int], as_of: date,
                   detail: str, severity: str = "warn") -> list[DataQualityFlag]:
    """Merge window indexes (any order) into contiguous flags."""
    out: list[DataQualityFlag] = []
    for k in sorted(set(windows), reverse=True):  # oldest first
        start, end = window_bounds(as_of, k)
        if out and out[-1].end + timedelta(days=1) == start:
            out[-1] = out[-1].model_copy(update={"end": end})
        else:
            out.append(DataQualityFlag(code=code, stream=stream, start=start, end=end,
                                       detail=detail, severity=severity))
    return out


def _implausible_weights(events: list[Event], cfg: Config) -> tuple[list[DataQualityFlag], set[str]]:
    limit = cfg.setting("data_quality.implausible_daily_weight_change_pct")
    flags, excluded = [], set()
    ref: tuple[date, float] | None = None
    for e in events:
        if e.type != "weigh_in":
            continue
        w = to_lb(e.payload.weight, e.payload.unit)
        if ref is not None:
            days = max(1, (e.day - ref[0]).days)
            pct_per_day = abs(w - ref[1]) / ref[1] * 100 / days
            if pct_per_day > limit:
                excluded.add(e.event_id)
                flags.append(DataQualityFlag(
                    code="implausible_weight", stream="weigh_in", start=e.day, end=e.day,
                    detail=f"{e.payload.weight}{e.payload.unit} is {pct_per_day:.1f}%/day from "
                           f"{ref[1]:.1f}lb on {ref[0]}; excluded from trend"))
                continue
        ref = (e.day, w)
    return flags, excluded


def assess(events: list[Event], as_of: date, cfg: Config,
           mesos: list[MesoSpan] | None = None) -> DQResult:
    events = [e for e in events if e.day <= as_of]
    res = DQResult()
    min_weigh_ins = cfg.nutrition("tracking.weigh_ins_per_week")[0]
    min_intake_days = cfg.setting("data_quality.min_intake_days_per_week")
    max_gap = cfg.setting("data_quality.max_gap_days")
    rir_lo, rir_hi = cfg.setting("data_quality.rir_valid_range")

    flags, excluded = _implausible_weights(events, cfg)
    res.flags += flags
    res.excluded_event_ids |= excluded

    by_type: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_type[e.type].append(e)

    # < N weigh-ins per week
    weigh = [e for e in by_type["weigh_in"] if e.event_id not in excluded]
    if weigh:
        first_k = window_index(as_of, weigh[0].day)
        counts = defaultdict(int)
        for e in weigh:
            counts[window_index(as_of, e.day)] += 1
        low = [k for k in range(first_k + 1) if counts[k] < min_weigh_ins]
        res.flags += _merge_windows("low_weigh_in_frequency", "weigh_in", low, as_of,
                                    f"fewer than {min_weigh_ins} weigh-ins per week")
    else:
        res.flags.append(DataQualityFlag(code="no_weigh_ins", stream="weigh_in", start=as_of,
                                         end=as_of, detail="no weigh-ins logged",
                                         severity="critical"))

    # intake on < N of 7 days (only once the client has started logging intake)
    intake = by_type["intake_logged"]
    if intake:
        first_k = window_index(as_of, intake[0].payload.date)
        days = defaultdict(set)
        for e in intake:
            days[window_index(as_of, e.payload.date)].add(e.payload.date)
        low = [k for k in range(first_k + 1) if len(days[k]) < min_intake_days]
        res.flags += _merge_windows("low_intake_logging", "intake", low, as_of,
                                    f"intake logged on fewer than {min_intake_days} of 7 days")

    # gaps > max_gap days in any stream, including a stale stream at as_of
    for etype, stream in GAP_STREAMS.items():
        evs = by_type[etype]
        if not evs:
            continue
        stamps = sorted({e.day for e in evs if e.event_id not in excluded})
        for a, b in zip(stamps, stamps[1:]):
            if (b - a).days > max_gap:
                res.flags.append(DataQualityFlag(
                    code="stream_gap", stream=stream, start=a, end=b,
                    detail=f"{(b - a).days}-day gap in {stream}"))
        if (as_of - stamps[-1]).days > max_gap:
            res.flags.append(DataQualityFlag(
                code="stream_gap", stream=stream, start=stamps[-1], end=as_of,
                detail=f"no {stream} for {(as_of - stamps[-1]).days} days", severity="critical"))

    # implausible RIR
    for e in by_type["set_logged"]:
        if not (rir_lo <= e.payload.rir <= rir_hi):
            res.excluded_event_ids.add(e.event_id)
            res.flags.append(DataQualityFlag(
                code="implausible_rir", stream="sets", start=e.day, end=e.day,
                detail=f"RIR {e.payload.rir} outside {rir_lo}-{rir_hi} "
                       f"({e.payload.exercise_id}); excluded"))

    # missing soreness: a window with sets for a muscle but no soreness rating.
    # The latest window is skipped: soreness for it is reported at the next session.
    sets_w: dict[str, set[int]] = defaultdict(set)
    for e in by_type["set_logged"]:
        sets_w[e.payload.muscle].add(window_index(as_of, e.day))
    sore_w: dict[str, set[int]] = defaultdict(set)
    for e in by_type["soreness_rated"]:
        sore_w[e.payload.muscle].add(window_index(as_of, e.day))
        sore_w[e.payload.muscle].add(window_index(as_of, e.day) + 1)  # refers to prior session
    for muscle in sorted(sets_w):
        missing = [k for k in sets_w[muscle] if k > 0 and k not in sore_w[muscle]]
        res.flags += _merge_windows("missing_soreness", "soreness", missing, as_of,
                                    f"no soreness ratings for {muscle}")

    # missing stimulus: required in meso week 1
    stim = {(e.payload.muscle, e.day) for e in by_type["stimulus_rated"]}
    for m in mesos or []:
        wk1_end = min(m.end, m.start + timedelta(days=7))
        trained = {e.payload.muscle for e in by_type["set_logged"]
                   if m.start <= e.day < wk1_end}
        rated = {mu for mu, d in stim if m.start <= d < wk1_end}
        for muscle in sorted(trained - rated):
            res.flags.append(DataQualityFlag(
                code="missing_stimulus", stream="stimulus", start=m.start,
                end=wk1_end - timedelta(days=1),
                detail=f"no stimulus ratings for {muscle} in week 1 of {m.meso_id}"))

    res.flags.sort(key=lambda f: (f.start, f.code, f.stream))
    return res
