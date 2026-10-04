"""Shared fixtures. Synthetic data only."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from config.loader import Config, load_config
from schemas.events import Event, make_event
from schemas.state import ClientState, PhaseState, WeeklyWeight, WeightTrend

CID = "SYN-T-01"


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config()


def ev(type_: str, day: date, payload: dict, source: str = "client", cid: str = CID) -> Event:
    return make_event(cid, type_, day, payload, source)


def weigh_ins(start: date, end: date, weight_fn, weekdays=(0, 2, 4)) -> list[Event]:
    out, d = [], start
    while d <= end:
        if d.weekday() in weekdays:
            out.append(ev("weigh_in", d, {"weight": round(weight_fn(d), 2), "unit": "lb"}))
        d += timedelta(days=1)
    return out


def trend(pct_bw_per_week: float, bw: float = 200.0, weeks: int = 3) -> WeightTrend:
    change = pct_bw_per_week / 100 * bw
    return WeightTrend(
        weekly=[WeeklyWeight(window_end=date(2026, 3, 31) - timedelta(days=7 * k),
                             avg_lb=bw - change * k, n=3) for k in range(weeks)],
        trend_weeks=weeks, avg_weekly_change_lb=change, pct_bw_per_week=pct_bw_per_week,
        latest_avg_lb=bw)


def state(phase: str | None = "cut", week: int = 5, target: float = 0.75, pct: float | None = None,
          adherence: float | None = 95, hunger: int = 3, **kw) -> ClientState:
    as_of = date(2026, 3, 31)
    st = ClientState(client_id=CID, as_of=as_of, **kw)
    if phase:
        st.phase = PhaseState(phase=phase, start_date=as_of - timedelta(days=7 * (week - 1)),
                              week=week, target_rate_pct_bw=target, planned_weeks=12,
                              source="coach")
    if pct is not None:
        st.weight = trend(pct)
    if adherence is not None:
        st.latest_checkin = {"adherence_pct": adherence, "hunger": hunger, "energy": 3,
                             "sleep": 3, "notes": "", "date": as_of}
    return st
