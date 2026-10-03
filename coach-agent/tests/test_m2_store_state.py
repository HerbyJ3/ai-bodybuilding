import sqlite3
from datetime import date, timedelta

import pytest

from engine.state_builder import build_state
from store.event_store import EventStore
from tests.conftest import CID, ev, weigh_ins

AS_OF = date(2026, 3, 31)


def test_append_read_dedupe_and_order():
    s = EventStore()
    e1 = ev("weigh_in", date(2026, 3, 2), {"weight": 200, "unit": "lb"})
    e2 = ev("weigh_in", date(2026, 3, 1), {"weight": 201, "unit": "lb"})
    assert s.append([e1, e2]) == (2, 0)
    assert s.append([e1]) == (0, 1)
    assert [e.event_id for e in s.read(CID)] == [e2.event_id, e1.event_id]
    assert s.read("other") == []


def test_store_is_append_only():
    s = EventStore()
    s.append([ev("weigh_in", date(2026, 3, 2), {"weight": 200, "unit": "lb"})])
    with pytest.raises(sqlite3.DatabaseError):
        s._db.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError):
        s._db.execute("UPDATE events SET client_id = 'x'")


def test_round_trip_preserves_payload(tmp_path):
    s = EventStore(tmp_path / "x.db")
    e = ev("intake_logged", date(2026, 3, 2),
           {"date": "2026-03-02", "calories": 2500, "protein_g": 200, "carb_g": 250, "fat_g": 70})
    s.append([e])
    assert s.read(CID)[0] == e


def test_weight_trend_math(cfg):
    # windows ending 03-31, 03-24, 03-17 average 200, 202, 203
    events = []
    for k, w in enumerate([200, 202, 203]):
        end = AS_OF - timedelta(days=7 * k)
        events += [ev("weigh_in", end - timedelta(days=i), {"weight": w, "unit": "lb"}) for i in (0, 3)]
    st = build_state(events, AS_OF, cfg)
    assert [w.avg_lb for w in st.weight.weekly[:3]] == [200, 202, 203]
    assert st.weight.trend_weeks == 3
    assert st.weight.avg_weekly_change_lb == pytest.approx(-1.5)
    assert st.weight.pct_bw_per_week == pytest.approx(-0.75)


def test_weight_trend_kg_converted(cfg):
    events = weigh_ins(AS_OF - timedelta(days=20), AS_OF, lambda d: 90.0)
    events = [ev("weigh_in", e.day, {"weight": 90, "unit": "kg"}) for e in events]
    st = build_state(events, AS_OF, cfg)
    assert st.weight.latest_avg_lb == pytest.approx(198.4158)


def test_one_weigh_in_this_week_no_trend(cfg):
    events = weigh_ins(AS_OF - timedelta(days=27), AS_OF - timedelta(days=7), lambda d: 200)
    events.append(ev("weigh_in", AS_OF, {"weight": 199, "unit": "lb"}))
    st = build_state(events, AS_OF, cfg)
    assert st.weight.weekly[0].n == 1
    assert st.weight.trend_weeks == 0 and st.weight.pct_bw_per_week is None


def test_phase_and_meso_week(cfg):
    events = [
        ev("phase_started", date(2026, 2, 17), {"phase": "cut", "target_rate_pct_bw": 0.75, "planned_weeks": 12}, "coach"),
        ev("meso_started", date(2026, 3, 17), {"meso_id": "m1", "weeks_planned": 5,
                                               "exercises_per_muscle": {"chest": ["bench"]},
                                               "starting_sets_per_muscle": {"chest": 6}}, "coach"),
    ]
    st = build_state(events, AS_OF, cfg)
    assert st.phase.week == 7
    assert st.meso.week == 3 and st.meso.rir_target == (2, 3) and not st.meso.is_deload_week


def test_state_is_deterministic_and_ignores_future(cfg):
    events = weigh_ins(AS_OF - timedelta(days=27), AS_OF + timedelta(days=7), lambda d: 200 - d.day / 10)
    a = build_state(events, AS_OF, cfg)
    b = build_state(list(reversed(events)), AS_OF, cfg)
    assert a == b
    assert all(w.window_end <= AS_OF for w in a.weight.weekly)


def test_maintenance_calibrated_from_history(cfg):
    # losing exactly 1 lb/week
    events = weigh_ins(AS_OF - timedelta(days=27), AS_OF, lambda d: 200 + (AS_OF - d).days / 7)
    for i in range(28):
        d = AS_OF - timedelta(days=i)
        events.append(ev("intake_logged", d, {"date": d, "calories": 2500, "protein_g": 200,
                                              "carb_g": 250, "fat_g": 70}))
    st = build_state(events, AS_OF, cfg)
    assert st.weight.avg_weekly_change_lb == pytest.approx(-1.0, abs=1e-6)
    # 2500 - (-1 * 3500 / 7) = 3000
    assert st.maintenance.kcal == 3000 and st.maintenance.confidence == "high"
