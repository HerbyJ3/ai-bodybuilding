import json
from datetime import date, timedelta

import pytest

from engine import history_review as hr
from engine.state_builder import build_state
from ingest.onboarding import OnboardingError, onboard
from schemas.events import SetLogged
from store.event_store import EventStore
from tests.conftest import CID, ev, weigh_ins
from tests.test_m6_import_onboarding import SAMPLE, _config, onboarded  # noqa: F401


def _s(load, reps):
    return SetLogged(exercise_id="e", muscle="m", load=load, reps=reps, rir=2, set_index=0, session_id="s")


@pytest.mark.parametrize("first,last,status", [
    (_s(100, 10), _s(100, 12), "progressed"),
    (_s(100, 10), _s(105, 10), "progressed"),
    (_s(100, 10), _s(100, 10), "flat"),
    (_s(100, 10), _s(100, 9), "regressed"),
    (_s(100, 10), _s(95, 10), "regressed"),
    (_s(100, 10), _s(105, 8), "mixed"),
    (_s(100, 10), _s(95, 12), "mixed"),
    (None, _s(100, 10), "insufficient_data"),
])
def test_compare_sets(first, last, status):
    assert hr.compare_sets(first, last) == status


def test_best_set_is_heaviest_then_most_reps():
    assert hr.best_set([_s(100, 12), _s(110, 6), _s(110, 8)]) == _s(110, 8)
    assert hr.best_set([]) is None


def test_phase_rate_vs_band_and_maintenance(cfg):
    start = date(2026, 1, 5)
    as_of = start + timedelta(days=7 * 4 - 1)  # 4 complete weeks
    events = [ev("phase_started", start, {"phase": "cut", "target_rate_pct_bw": 0.75, "planned_weeks": 12}, "coach")]
    # lose exactly 1 lb per phase week from 200 lb
    events += weigh_ins(start, as_of, lambda d: 200 - (d - start).days // 7)
    for i in range(28):
        d = start + timedelta(days=i)
        events.append(ev("intake_logged", d, {"date": d, "calories": 2000, "protein_g": 200,
                                              "carb_g": 150, "fat_g": 60}))
    st = build_state(events, as_of, cfg)
    [p] = hr.review(events, st, cfg)["phases"]
    assert p["complete_weeks"] == 4 and p["weeks_with_enough_weigh_ins"] == 4
    assert p["avg_rate_pct_bw"] == pytest.approx(-0.5)  # -1 lb/wk from 200 lb
    assert p["rate_vs_band"] == "within"
    assert p["estimated_maintenance_kcal"] == 2500  # 2000 + 1 lb * 3500 / 7
    assert [r["week"] for r in p["weekly_rate_pct_bw"]] == [2, 3, 4]


def test_sample_review_findings(onboarded):  # noqa: F811
    _, report = onboarded
    rev = report["history_review"]
    t = rev["training"]
    assert [m["complete"] for m in t["mesos"]] == [True, True, True, False]

    rot = {r["exercise"] for r in t["rotation_candidates"]}
    assert "lat_pulldown" in rot
    # the in-progress meso (2 weeks) is never the basis for a rotation call
    assert all(r["latest_meso"] != "SYN-CUT-01-m4" for r in t["rotation_candidates"])

    quads_m3 = next(v for v in t["volume_by_muscle"]
                    if v["muscle"] == "quads" and v["meso_id"] == "SYN-CUT-01-m3")
    assert quads_m3["performance_dropped"] == {"week": 5, "sets": 10}

    # like-for-like: current meso compared at its latest week (2), not the old meso's peak
    m3_m4 = [r for r in t["exercise_progress_across_mesos"] if r["to_meso"] == "SYN-CUT-01-m4"]
    assert m3_m4 and all(r["meso_week"] == 2 for r in m3_m4)

    cut = next(p for p in rev["phases"] if p["phase"] == "cut")
    assert cut["rate_vs_band"] == "below" and cut["avg_rate_pct_bw"] < 0
    assert "estimated_maintenance_kcal" in cut
    assert cut["checkins"]["hunger_first_last"][1] > cut["checkins"]["hunger_first_last"][0]

    habits = rev["data_habits"]["streams"]
    assert habits["soreness_rated"]["collected"] is False and habits["weigh_in"]["collected"] is True

    text = " ".join(f["finding"] for f in rev["findings"])
    for needle in ("below the 0.5–1.0% band", "lat_pulldown (back)", "possible MRV",
                   "optional, not collected (would sharpen coaching): soreness ratings", "hunger rose"):
        assert needle in text, needle


def test_review_is_read_only(onboarded, cfg):  # noqa: F811
    store, _ = onboarded
    before = store.read("SYN-CUT-01")
    st = build_state(before, date(2026, 9, 28), cfg)
    hr.review(before, st, cfg)
    assert store.read("SYN-CUT-01") == before


def test_past_mesos_create_backdated_import_events(onboarded):  # noqa: F811
    store, _ = onboarded
    mesos = [e for e in store.read("SYN-CUT-01") if e.type == "meso_started"]
    assert [(e.payload.meso_id, e.day, e.source.value) for e in mesos] == [
        ("SYN-CUT-01-m1", date(2026, 5, 18), "import"),
        ("SYN-CUT-01-m2", date(2026, 6, 29), "import"),
        ("SYN-CUT-01-m3", date(2026, 8, 3), "import"),
        ("SYN-CUT-01-m4", date(2026, 9, 14), "import")]
    phases = [e for e in store.read("SYN-CUT-01") if e.type == "phase_started"]
    assert [e.payload.phase for e in phases] == ["maintenance", "cut"]


@pytest.mark.parametrize("override,match", [
    ({"past_mesos": [{"meso_id": "x", "start_date": "2026-09-20", "weeks_planned": 4}]}, "on/after the current meso"),
    ({"past_mesos": [{"meso_id": "SYN-CUT-01-m4", "start_date": "2026-08-03", "weeks_planned": 4}]}, "duplicates"),
    ({"past_phases": [{"phase": "gain", "start_date": "2026-09-01", "target_rate_pct_bw": 0.25,
                       "planned_weeks": 8}]}, "on/after the current phase"),
])
def test_past_structure_validation(cfg, override, match):
    with pytest.raises(OnboardingError, match=match):
        onboard(EventStore(), _config(**override), cfg, base_dir=SAMPLE)


def test_review_without_past_mesos_still_works(cfg):
    rep = onboard(EventStore(), _config(past_mesos=[], past_phases=[]), cfg, base_dir=SAMPLE).data
    rev = json.loads(json.dumps(rep["history_review"], default=str))
    assert [m["meso_id"] for m in rev["training"]["mesos"]] == ["SYN-CUT-01-m4"]
    assert [p["phase"] for p in rev["phases"]] == ["cut"]


def test_cli_review(tmp_path):
    from typer.testing import CliRunner
    from ingest.cli import app
    db = tmp_path / "coach.db"
    assert CliRunner().invoke(app, ["onboard", str(SAMPLE / "onboarding.json"), "--db", str(db)]).exit_code == 0
    r = CliRunner().invoke(app, ["review", "SYN-CUT-01", "2026-09-28", "--db", str(db)])
    assert r.exit_code == 0 and "[training]" in r.output and "rotation candidate" in r.output
