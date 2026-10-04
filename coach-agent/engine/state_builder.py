"""Rebuilds ClientState from events (BUILD_SPEC §6.2, M2). Pure: same events
and as_of always produce the same state."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from config.loader import Config
from engine import data_quality
from engine.data_quality import MesoSpan
from engine.nutrition.maintenance import calibrate
from engine.training.mesocycle import rir_target
from engine.training.performance import muscle_score
from engine.util import to_lb, week_index, window_bounds, window_index
from schemas.events import Event, SetLogged
from schemas.state import (ClientState, Limitation, MesoState, MuscleWeek, PhaseState,
                           TargetChange, WeeklyWeight, WeightTrend)

TREND_HISTORY_WEEKS = 8  # windows kept on the state for display; not a rule input


def _weight_trend(weigh: list[Event], as_of: date, cfg: Config) -> WeightTrend:
    min_n = cfg.nutrition("tracking.weigh_ins_per_week")[0]
    assess_max = cfg.nutrition("tracking.assess_window_weeks")[1]
    buckets: dict[int, list[float]] = defaultdict(list)
    for e in weigh:
        buckets[window_index(as_of, e.day)].append(to_lb(e.payload.weight, e.payload.unit))
    weekly = []
    for k in range(TREND_HISTORY_WEEKS):
        vals = buckets.get(k, [])
        weekly.append(WeeklyWeight(window_end=window_bounds(as_of, k)[1],
                                   avg_lb=sum(vals) / len(vals) if vals else None, n=len(vals)))
    trend_weeks = 0
    while trend_weeks < len(weekly) and weekly[trend_weeks].n >= min_n:
        trend_weeks += 1
    t = WeightTrend(weekly=weekly, trend_weeks=trend_weeks,
                    latest_avg_lb=weekly[0].avg_lb)
    if trend_weeks >= 2:
        span = min(trend_weeks, assess_max) - 1
        change = (weekly[0].avg_lb - weekly[span].avg_lb) / span  # type: ignore[operator]
        t.avg_weekly_change_lb = change
        t.pct_bw_per_week = change / weekly[0].avg_lb * 100  # type: ignore[operator]
    return t


def _meso_spans(mesos: list[Event], as_of: date) -> list[MesoSpan]:
    spans = []
    for i, e in enumerate(mesos):
        end = mesos[i + 1].day if i + 1 < len(mesos) else as_of + timedelta(days=1)
        spans.append(MesoSpan(e.payload.meso_id, e.day, end, e.payload.weeks_planned))
    return spans


def _muscle_weeks(events: list[Event], spans: list[MesoSpan], excluded: set[str],
                  cfg: Config) -> dict[str, list[MuscleWeek]]:
    def locate(d: date) -> tuple[MesoSpan, int] | None:
        for s in spans:
            if s.start <= d < s.end:
                return s, week_index(s.start, d)
        return None

    sets: dict[tuple[str, str, int], dict[str, list[SetLogged]]] = defaultdict(lambda: defaultdict(list))
    session_loc: dict[str, tuple[MesoSpan, int]] = {}
    cells: dict[tuple[str, str, int], MuscleWeek] = {}

    def cell(muscle: str, span: MesoSpan, wk: int) -> MuscleWeek:
        key = (muscle, span.meso_id, wk)
        if key not in cells:
            cells[key] = MuscleWeek(meso_id=span.meso_id, meso_week=wk,
                                    week_start=span.start + timedelta(days=7 * (wk - 1)))
        return cells[key]

    for e in events:
        if e.type != "set_logged" or e.event_id in excluded:
            continue
        loc = locate(e.day)
        if not loc:
            continue
        p = e.payload
        session_loc.setdefault(p.session_id, loc)
        sets[(p.muscle, loc[0].meso_id, loc[1])][p.exercise_id].append(p)
        cell(p.muscle, *loc).sets += 1

    for e in events:
        if e.type == "soreness_rated":
            loc = session_loc.get(e.payload.refers_to_session_id or "") or locate(e.day)
            if loc:
                c = cell(e.payload.muscle, *loc)
                c.soreness = max(c.soreness or 0, e.payload.soreness)
        elif e.type == "stimulus_rated":
            loc = session_loc.get(e.payload.session_id) or locate(e.day)
            if loc:
                c = cell(e.payload.muscle, *loc)
                total = e.payload.mind_muscle + e.payload.pump + e.payload.disruption
                c.stimulus_total = max(c.stimulus_total or 0, total)

    by_span = {s.meso_id: s for s in spans}
    for (muscle, meso_id, wk), c in cells.items():
        if wk >= 2:
            span = by_span[meso_id]
            c.performance = muscle_score(sets.get((muscle, meso_id, wk), {}),
                                         sets.get((muscle, meso_id, wk - 1), {}),
                                         rir_target(wk, span.weeks_planned, cfg))
    out: dict[str, list[MuscleWeek]] = defaultdict(list)
    for (muscle, _, _), c in sorted(cells.items(), key=lambda kv: (kv[0][0], kv[1].week_start)):
        out[muscle].append(c)
    return dict(out)


def build_state(events: list[Event], as_of: date, cfg: Config) -> ClientState:
    if not events:
        raise ValueError("no events")
    client_id = events[0].client_id
    events = sorted((e for e in events if e.day <= as_of),
                    key=lambda e: (e.timestamp, e.recorded_at, e.event_id))
    by_type: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_type[e.type].append(e)
    latest = lambda t: by_type[t][-1] if by_type[t] else None  # noqa: E731

    meso_events = by_type["meso_started"]
    spans = _meso_spans(meso_events, as_of)
    dq = data_quality.assess(events, as_of, cfg, spans)
    weigh = [e for e in by_type["weigh_in"] if e.event_id not in dq.excluded_event_ids]

    st = ClientState(client_id=client_id, as_of=as_of)
    if (c := latest("consent_recorded")):
        st.consent = c.payload.granted
    for e in by_type["profile_updated"]:
        for f in ("training_age", "cardio_max_sessions_per_week", "cardio_max_minutes_per_session"):
            if getattr(e.payload, f) is not None:
                setattr(st, f, getattr(e.payload, f))
    if (o := latest("onboarding_completed")):
        st.onboarded, st.onboarding_date = True, o.payload.as_of

    if (p := latest("phase_started")):
        st.phase = PhaseState(phase=p.payload.phase, start_date=p.day,
                              week=week_index(p.day, as_of),
                              target_rate_pct_bw=p.payload.target_rate_pct_bw,
                              planned_weeks=p.payload.planned_weeks, source=p.source.value)

    st.weight = _weight_trend(weigh, as_of, cfg)
    # One intake per day: a later log for the same date (e.g. a re-exported, edited diary) wins.
    latest_intake = {e.payload.date: e.payload.calories for e in by_type["intake_logged"]}
    intake = sorted(latest_intake.items())
    st.maintenance = calibrate(st.weight, intake, as_of, cfg)
    recent = [k for d, k in intake if 0 <= (as_of - d).days < 14]
    st.avg_intake_kcal = round(sum(recent) / len(recent)) if recent else None
    macros: dict = {}
    for n in by_type["nutrition_targets_set"]:  # merged per day type, in order
        for day_type, m in n.payload.macros_by_day_type.items():
            if macros.get(day_type) != m:
                st.target_changes.append(TargetChange(
                    date=n.day, day_type=day_type, before=macros.get(day_type), after=m,
                    note=n.payload.note, source=n.source.value))
            macros[day_type] = m
    st.current_macros = macros or None
    lims: dict[str, Limitation] = {}
    for e in by_type["limitation_recorded"]:
        p = e.payload
        if p.active:
            lims[p.limitation_id] = Limitation(limitation_id=p.limitation_id, area=p.area,
                                               description=p.description,
                                               restrictions=list(p.restrictions), since=e.day)
        else:
            lims.pop(p.limitation_id, None)
    st.limitations = list(lims.values())

    if (d := latest("deload_completed")):
        st.last_deload_end = d.payload.end_date
    if meso_events:
        m = meso_events[-1]
        wk = week_index(m.day, as_of)
        st.meso = MesoState(
            meso_id=m.payload.meso_id, start_date=m.day, week=wk,
            weeks_planned=m.payload.weeks_planned,
            rir_target=rir_target(wk, m.payload.weeks_planned, cfg),
            is_deload_week=wk > m.payload.weeks_planned,
            exercises_per_muscle=m.payload.exercises_per_muscle,
            starting_sets_per_muscle=m.payload.starting_sets_per_muscle,
            source=m.source.value)
    st.muscle_weeks = _muscle_weeks(events, spans, dq.excluded_event_ids, cfg)

    for e in by_type["joint_pain_reported"]:
        if (as_of - e.day).days < 7:
            st.joint_pain[e.payload.joint] = max(st.joint_pain.get(e.payload.joint, 0),
                                                 e.payload.severity)
    checkins = [dict(e.payload.model_dump(), date=e.day) for e in by_type["weekly_checkin"]]
    st.checkins_recent = list(reversed(checkins))[:4]
    st.latest_checkin = st.checkins_recent[0] if st.checkins_recent else None
    cardio = defaultdict(float)
    for e in by_type["cardio_logged"]:
        cardio[window_index(as_of, e.payload.date)] += e.payload.minutes
    st.cardio_minutes_by_week = [cardio[k] for k in range(4)]
    lookback = cfg.setting("cardio_lever.lookback_days")
    st.cardio_recent = [{"date": e.payload.date, "minutes": e.payload.minutes,
                         "intensity": e.payload.intensity, "modality": e.payload.modality,
                         "est_kcal": e.payload.est_kcal}
                        for e in by_type["cardio_logged"] if 0 <= (as_of - e.payload.date).days < lookback]
    st.data_quality = dq.flags
    return st
