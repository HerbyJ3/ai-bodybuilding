"""Body limitations and macro-target history (BUILD_SPEC §13.1, §14). Synthetic data only."""
import json
from datetime import date, timedelta

import pytest

from engine import history_review as hr
from engine.state_builder import build_state
from ingest.csv_import import import_csvs, load_mapping
from ingest.onboarding import onboard
from llm.coach import open_session
from store.event_store import EventStore
from tests.conftest import CID, ev, weigh_ins
from tests.test_m6_import_onboarding import SAMPLE, _config

AS_OF = date(2026, 3, 31)
M = lambda p, c, f: {"protein_g": p, "carb_g": c, "fat_g": f}  # noqa: E731


def test_target_csv_rows_grouped_by_date(tmp_path):
    res = import_csvs("C", SAMPLE, load_mapping(SAMPLE / "mapping.json"))
    targets = [e for e in res.events if e.type == "nutrition_targets_set"]
    assert [e.day for e in targets] == [date(2026, 6, 8), date(2026, 8, 17), date(2026, 9, 7)]
    assert set(targets[0].payload.macros_by_day_type) == {"moderate", "non_training"}
    assert targets[1].payload.note == "cut start; cut start"
    assert all(e.source.value == "import" for e in targets)


def test_target_csv_bad_row_rejected(tmp_path):
    (tmp_path / "t.csv").write_text("D,T,P,C,F\n2026-01-01,moderate,200,250,70\n2026-01-08,bogus,200,250,70\n")
    mapping = {"streams": {"nutrition_targets_set": {"file": "t.csv", "date_column": "D", "fields": {
        "day_type": {"column": "T"}, "protein_g": {"column": "P"}, "carb_g": {"column": "C"},
        "fat_g": {"column": "F"}}}}}
    res = import_csvs("C", tmp_path, mapping)
    assert len(res.events) == 1 and res.streams["nutrition_targets_set"].rejected[0]["row"] == [3]


def test_targets_merge_per_day_type_and_history(cfg):
    events = [
        ev("nutrition_targets_set", date(2026, 3, 1), {"macros_by_day_type": {
            "moderate": M(220, 215, 65), "non_training": M(220, 130, 70)}}, "coach"),
        ev("nutrition_targets_set", date(2026, 3, 14), {"macros_by_day_type": {
            "moderate": M(220, 190, 65)}, "note": "carb drop"}, "coach"),
    ]
    st = build_state(events, AS_OF, cfg)
    assert st.current_macros["moderate"].carb_g == 190
    assert st.current_macros["non_training"].carb_g == 130  # untouched day type kept
    changed = [c for c in st.target_changes if c.before is not None]
    assert [(c.date, c.day_type, c.before.carb_g, c.after.carb_g, c.note) for c in changed] == [
        (date(2026, 3, 14), "moderate", 215, 190, "carb drop")]


def test_macro_change_review_trend_before_and_after(cfg):
    change = date(2026, 3, 9)
    events = [
        ev("nutrition_targets_set", date(2026, 2, 16), {"macros_by_day_type": {"moderate": M(220, 215, 65)}}, "coach"),
        ev("nutrition_targets_set", change, {"macros_by_day_type": {"moderate": M(220, 190, 65)}}, "coach"),
    ]
    # flat at 210 for 3 weeks before; then -1 lb per week for 3 weeks
    events += weigh_ins(change - timedelta(days=21), change - timedelta(days=1), lambda d: 210.0)
    events += weigh_ins(change, change + timedelta(days=20), lambda d: 210.0 - (d - change).days // 7)
    st = build_state(events, change + timedelta(days=20), cfg)
    [c] = hr.review(events, st, cfg)["macro_changes"]
    assert c["change_g"] == {"protein_g": 0, "carb_g": -25, "fat_g": 0} and c["kcal_change"] == -100
    assert (c["lb_per_week_before"], c["weeks_before"]) == (0.0, 3)
    assert (c["lb_per_week_after"], c["weeks_after"]) == (-1.0, 3)


def test_limitation_recorded_and_resolved(cfg):
    lim = {"limitation_id": "neck", "area": "cervical spine", "restrictions": ["no free weights"]}
    events = [ev("limitation_recorded", date(2026, 3, 1), lim, "coach")]
    assert [x.restrictions for x in build_state(events, AS_OF, cfg).limitations] == [["no free weights"]]
    events.append(ev("limitation_recorded", date(2026, 3, 20), {**lim, "active": False}, "coach"))
    assert build_state(events, AS_OF, cfg).limitations == []


def test_limitation_requires_a_restriction():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ev("limitation_recorded", AS_OF, {"limitation_id": "x", "area": "knee", "restrictions": []})


def test_sample_limitation_reaches_review_and_prompt(cfg):
    store = EventStore()
    rep = onboard(store, _config(), cfg, base_dir=SAMPLE).data
    assert rep["history_review"]["limitations"][0]["area"] == "right shoulder (synthetic)"
    text = " ".join(f["finding"] for f in rep["history_review"]["findings"])
    assert "no overhead pressing" in text and "moderate targets: carb 300→230g (-280 kcal/day)" in text
    session = open_session(store, cfg, "SYN-CUT-01", date(2026, 9, 28), llm=None)
    assert "limitations:" in session.system and "no overhead pressing" in session.system
    assert "Limitations are hard constraints" in session.system
    assert "macro_target_history:" in session.system


def test_onboarding_without_meso(cfg):
    store = EventStore()
    rep = onboard(store, _config(meso=None, past_mesos=[]), cfg, base_dir=SAMPLE).data
    assert rep["meso"] is None
    assert any("no mesocycle given" in w for w in rep["warnings"])
    events = store.read("SYN-CUT-01")
    assert not [e for e in events if e.type == "meso_started"]
    done = next(e for e in events if e.type == "onboarding_completed").payload
    assert done.meso_id is None and done.current_meso_week is None
    assert not [p for p in rep["proposals"] if p["rule_id"].startswith("training.")]
    assert [p["rule_id"] for p in rep["proposals"] if p["rule_id"].startswith("nutrition")]


def test_duplicate_limitation_ids_rejected(cfg):
    from ingest.onboarding import OnboardingError
    lim = {"limitation_id": "a", "area": "knee", "restrictions": ["no jumping"]}
    with pytest.raises(OnboardingError, match="duplicate"):
        onboard(EventStore(), _config(limitations=[lim, lim]), cfg, base_dir=SAMPLE)


def test_onboarding_cardio_ceilings_reach_state(cfg):
    store = EventStore()
    onboard(store, _config(cardio_max_sessions_per_week=3, cardio_max_minutes_per_session=40), cfg,
            base_dir=SAMPLE)
    st = build_state(store.read("SYN-CUT-01"), date(2026, 9, 28), cfg)
    assert (st.cardio_max_sessions_per_week, st.cardio_max_minutes_per_session) == (3, 40)
    assert st.cardio_recent and all(s["modality"] == "incline walk" for s in st.cardio_recent)


def test_onboarding_warns_when_day_type_looks_wrong(cfg):
    low = {"moderate": {"protein_g": 190, "carb_g": 120, "fat_g": 65},
           "non_training": {"protein_g": 190, "carb_g": 150, "fat_g": 70}}
    rep = onboard(EventStore(), _config(nutrition_targets=low), cfg, base_dir=SAMPLE).data
    assert any("below the book minimum for 'moderate'" in w for w in rep["warnings"])
    nut = [p for p in rep["proposals"] if p["rule_id"].startswith("nutrition")]
    assert "carbs_below_day_type_minimum:moderate" in nut[0]["flags"]


def test_onboarding_reports_minimum_and_optional_data(cfg):
    rep = onboard(EventStore(), _config(), cfg, base_dir=SAMPLE).data
    assert rep["minimum_data"]["weigh_ins"]["ok"] and rep["minimum_data"]["macro_targets"]["ok"]
    assert "soreness ratings" in rep["optional_data_missing"]
    assert "weigh-ins" not in rep["optional_data_missing"]


def test_onboarding_warns_when_minimum_missing(cfg, tmp_path):
    import shutil
    for f in SAMPLE.iterdir():
        if f.is_file():
            shutil.copy(f, tmp_path / f.name)
    m = json.loads((tmp_path / "mapping.json").read_text())
    del m["streams"]["nutrition_targets_set"]
    (tmp_path / "mapping.json").write_text(json.dumps(m))
    rep = onboard(EventStore(), _config(nutrition_targets=None), cfg, base_dir=tmp_path).data
    assert not rep["minimum_data"]["macro_targets"]["ok"]
    assert any("no macro targets" in w for w in rep["warnings"])
    nut = [p for p in rep["proposals"] if p["rule_id"].startswith("nutrition.weekly")]
    assert nut[0]["action"] == "hold" and "no_current_targets" in nut[0]["flags"]
