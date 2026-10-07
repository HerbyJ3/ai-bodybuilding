import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine import proposals
from engine.state_builder import build_state
from ingest.csv_import import MappingError, import_csvs, load_mapping
from ingest.onboarding import OnboardingError, load_onboarding_config, onboard
from samples import generate_mid_cut_client as gen
from schemas.events import Source, make_event
from store.event_store import EventStore

SAMPLE = Path(gen.__file__).resolve().parent / "mid_cut_client"
CID = "SYN-CUT-01"
AS_OF = date(2026, 9, 28)


@pytest.fixture()
def onboarded(cfg):
    store = EventStore()
    oc = load_onboarding_config(SAMPLE / "onboarding.json")
    report = onboard(store, oc, cfg, base_dir=SAMPLE)
    return store, json.loads(report.to_json())


# --- sample data ----------------------------------------------------------------

def test_committed_sample_matches_generator(tmp_path):
    gen.write(tmp_path)
    for name in gen.generate():
        assert (tmp_path / name).read_bytes() == (SAMPLE / name).read_bytes(), name


# --- CSV import -------------------------------------------------------------------

def test_import_with_mapping(cfg):
    res = import_csvs(CID, SAMPLE, load_mapping(SAMPLE / "mapping.json"))
    assert all(e.source is Source.import_ for e in res.events)
    counts = res.counts()
    assert counts["weigh_in"] == 42 and counts["set_logged"] == 582
    assert counts["session_completed"] == len({e.payload.session_id for e in res.events
                                                if e.type == "set_logged"})
    rej = res.streams["weekly_checkin"].rejected
    assert len(rej) == 1 and rej[0]["row"] == 4  # hunger 7
    w = next(e for e in res.events if e.type == "weigh_in")
    assert w.payload.unit == "lb" and w.payload.conditions == "fasted, post-bathroom"


def test_reimport_is_idempotent():
    store = EventStore()
    mapping = load_mapping(SAMPLE / "mapping.json")
    first = store.append(import_csvs(CID, SAMPLE, mapping).events)
    second = store.append(import_csvs(CID, SAMPLE, mapping).events)
    assert second == (0, first[0])


def test_bad_mapping_rejected(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"streams": {"weigh_in": {"file": "x.csv", "date_column": "D",
                                                      "fields": {"kilos": {"column": "K"}}}}}))
    with pytest.raises(MappingError):
        load_mapping(p)
    p.write_text(json.dumps({"streams": {"phase_started": {"file": "x.csv", "date_column": "D"}}}))
    with pytest.raises(MappingError):
        load_mapping(p)


def test_missing_column_and_file_reported(tmp_path):
    (tmp_path / "w.csv").write_text("Date,Wt\n2026-01-01,190\n")
    mapping = {"streams": {"weigh_in": {"file": "w.csv", "date_column": "Date",
                                        "fields": {"weight": {"column": "Weight"}, "unit": {"value": "lb"}}},
                           "intake_logged": {"file": "none.csv", "date_column": "Date", "fields": {}}}}
    res = import_csvs(CID, tmp_path, mapping)
    assert res.events == []
    assert "Weight" in res.streams["weigh_in"].rejected[0]["error"]
    assert "file not found" in res.streams["intake_logged"].rejected[0]["error"]


# --- onboarding: backdated structure -----------------------------------------------

def test_backdated_events_with_import_source(onboarded):
    store, report = onboarded
    events = store.read(CID)
    by_type = {e.type: e for e in events}
    phase, meso, deload = by_type["phase_started"], by_type["meso_started"], by_type["deload_completed"]
    assert phase.source is Source.import_ and meso.source is Source.import_ and deload.source is Source.import_
    assert phase.day == date(2026, 8, 17)  # week 7 of the cut on 2026-09-28
    assert meso.day == date(2026, 9, 14)   # week 3 of the meso on 2026-09-28
    assert deload.payload.end_date == date(2026, 9, 13)
    assert deload.payload.start_date == date(2026, 9, 7)
    assert phase.recorded_at > phase.timestamp  # written now, effective in the past
    assert by_type["consent_recorded"].source is Source.coach
    done = by_type["onboarding_completed"].payload
    assert (done.current_meso_week, done.last_deload_date) == (3, date(2026, 9, 13))
    assert report["meso"]["inferred"]  # exercises/sets inferred from meso week 1 logs
    assert meso.payload.exercises_per_muscle["chest"] == ["bench_press", "incline_db_press"]
    assert meso.payload.starting_sets_per_muscle["chest"] == 6


def test_rir_schedule_resumes(onboarded, cfg):
    store, report = onboarded
    assert report["meso"]["rir_target_this_week"] == [2, 3]
    assert report["meso"]["rir_target_next_week"] == [1, 2]
    events = store.read(CID)
    for days, week, rir in [(0, 3, (2, 3)), (7, 4, (1, 2)), (14, 5, (0, 1)), (21, 6, None)]:
        st = build_state(events, AS_OF + timedelta(days=days), cfg)
        assert (st.meso.week, st.meso.rir_target) == (week, rir)
        assert st.phase.week == 7 + days // 7
    st = build_state(events, AS_OF + timedelta(days=14), cfg)
    [deload] = [p for p in proposals.generate(st, cfg) if p.rule_id == "training.fatigue.deload"]
    assert "final accumulation week" in deload.rationale_short
    # no logs since onboarding -> streams stale -> low confidence: surfaced as a flag
    assert deload.confidence == "low" and deload.action == "flag"
    assert deload.inputs_used["low_confidence_suppressed_action"] == "deload"


def test_data_quality_report_surfaces_gaps(onboarded):
    _, report = onboarded
    dq = report["data_quality"]
    gap_streams = {(g["stream"], g["start"]) for g in dq["gaps"]}
    for stream in ("weigh_in", "intake", "sets"):
        assert any(s == stream and start.startswith("2026-07") for s, start in gap_streams), stream
    codes = dq["counts_by_code"]
    for code in ("implausible_weight", "low_weigh_in_frequency", "low_intake_logging",
                 "missing_soreness", "stream_gap"):
        assert codes.get(code), code
    implausible = [f for f in dq["flags"] if f["code"] == "implausible_weight"]
    assert implausible[0]["start"] == "2026-06-24"
    assert report["import"]["weekly_checkin"]["rejected"]


# --- onboarding: cold start -----------------------------------------------------------

def test_cold_start_nutrition_runs_training_holds(onboarded):
    _, report = onboarded
    cs = report["cold_start"]
    assert cs["nutrition"]["ready"] is True and cs["nutrition"]["weeks_of_weigh_ins"] >= 2
    assert cs["training_gate_applies"] is True
    assert all(not m["ready"] and m["rated_weeks"] == 0 for m in cs["training"].values())
    training = [p for p in report["proposals"] if p["target"].startswith("volume.")]
    assert {p["target"] for p in training} == {f"volume.{m}" for m in cs["training"]}
    assert all(p["action"] == "hold" and "cold_start_training" in p["flags"] for p in training)
    nutrition = [p for p in report["proposals"] if p["rule_id"].startswith("nutrition")]
    # losing ~0.3 %/wk on a 0.75 %/wk cut target with good adherence
    assert [p["action"] for p in nutrition] == ["decrease_calories"]


def test_nutrition_cold_start_holds_without_two_weeks_of_weigh_ins(cfg, tmp_path):
    oc = load_onboarding_config(SAMPLE / "onboarding.json")
    for f in SAMPLE.glob("*.csv"):
        (tmp_path / f.name).write_text(f.read_text())
    (tmp_path / "mapping.json").write_text((SAMPLE / "mapping.json").read_text())
    lines = (tmp_path / "weigh_ins.csv").read_text().splitlines()
    (tmp_path / "weigh_ins.csv").write_text("\n".join([lines[0], lines[-1]]) + "\n")
    report = json.loads(onboard(EventStore(), oc, cfg, base_dir=tmp_path).to_json())
    assert report["cold_start"]["nutrition"]["ready"] is False
    nutrition = [p for p in report["proposals"] if p["rule_id"].startswith("nutrition")]
    assert [(p["rule_id"], p["action"]) for p in nutrition] == [("nutrition.cold_start", "hold")]


def _post_onboarding_week(week_start: date, wk: int, soreness: int) -> list:
    """One meso week of synthetic sets + soreness for every muscle, logged by the client."""
    out = []
    for wd, (name, lifts) in gen.SESSIONS.items():
        d = week_start + timedelta(days=wd)
        sid = f"{d}-{name}"
        for muscle, ex, load, reps in lifts:
            out.append(make_event(CID, "soreness_rated", d, {"muscle": muscle, "soreness": soreness}))
            for s in range(3 + (wk - 1) // 2):
                out.append(make_event(CID, "set_logged", d, {
                    "exercise_id": ex, "muscle": muscle, "load": load, "reps": reps + wk - 1 - s // 2,
                    "rir": {3: 2, 4: 1}[wk], "set_index": s, "session_id": sid}))
    return out


def test_training_gate_opens_after_two_rated_weeks(onboarded, cfg):
    store, _ = onboarded
    store.append(_post_onboarding_week(AS_OF, 3, soreness=0))
    st = build_state(store.read(CID), AS_OF + timedelta(days=7), cfg)
    props = [p for p in proposals.generate(st, cfg) if p.target == "volume.chest"]
    assert props[0].action == "hold" and "cold_start_training" in props[0].data_quality_flags
    assert props[0].inputs_used["rated_weeks"] == 1

    store.append(_post_onboarding_week(AS_OF + timedelta(days=7), 4, soreness=0))
    st = build_state(store.read(CID), AS_OF + timedelta(days=14), cfg)
    props = [p for p in proposals.generate(st, cfg) if p.target == "volume.chest"]
    assert len(props) == 1 and "cold_start_training" not in props[0].data_quality_flags
    assert props[0].rule_id == "training.set_progression"
    assert props[0].inputs_used["soreness"] == 0


def test_training_gate_not_applied_to_fresh_clients(cfg):
    from engine import cold_start
    st = build_state([make_event("fresh", "weigh_in", AS_OF, {"weight": 180, "unit": "lb"})], AS_OF, cfg)
    assert not cold_start.training_applies(st, cfg)


# --- onboarding: validation ----------------------------------------------------------------

def _config(**overrides):
    raw = json.loads((SAMPLE / "onboarding.json").read_text())
    for path, value in overrides.items():
        node = raw
        *parents, leaf = path.split(".")
        for p in parents:
            node = node[p]
        node[leaf] = value
    from schemas.onboarding import OnboardingConfig
    return OnboardingConfig.model_validate(raw)


def test_requires_consent(cfg):
    store = EventStore()
    with pytest.raises(OnboardingError, match="consent"):
        onboard(store, _config(**{"consent.granted": False}), cfg, base_dir=SAMPLE)
    assert store.read(CID) == []


def test_rejects_meso_week_beyond_plan(cfg):
    with pytest.raises(OnboardingError, match="beyond"):
        onboard(EventStore(), _config(**{"meso.current_week": 7}), cfg, base_dir=SAMPLE)


def test_rejects_deload_inside_current_meso(cfg):
    with pytest.raises(OnboardingError, match="deload"):
        onboard(EventStore(), _config(**{"meso.last_deload_date": "2026-09-20"}), cfg, base_dir=SAMPLE)


def test_warns_when_deload_far_from_meso_start(cfg):
    rep = onboard(EventStore(), _config(**{"meso.last_deload_date": "2026-08-01"}), cfg, base_dir=SAMPLE)
    assert any("between last deload and meso start" in w for w in rep.data["warnings"])


def test_explicit_meso_layout_not_inferred(cfg):
    rep = onboard(EventStore(), _config(**{"meso.exercises_per_muscle": {"chest": ["bench_press"]},
                                           "meso.starting_sets_per_muscle": {"chest": 4}}),
                  cfg, base_dir=SAMPLE)
    assert rep.data["meso"]["inferred"] == []


def test_onboarding_is_idempotent(cfg):
    store = EventStore()
    oc = load_onboarding_config(SAMPLE / "onboarding.json")
    first = onboard(store, oc, cfg, base_dir=SAMPLE).data
    second = onboard(store, oc, cfg, base_dir=SAMPLE).data
    assert second["events_written"] == 0
    assert second["duplicates_ignored"] == first["events_written"]


def test_cli_onboard(tmp_path):
    from typer.testing import CliRunner
    from ingest.cli import app
    db = tmp_path / "coach.db"
    out = tmp_path / "report.json"
    r = CliRunner().invoke(app, ["onboard", str(SAMPLE / "onboarding.json"), "--db", str(db),
                                 "--report", str(out)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["meso"]["current_week"] == 3
    r = CliRunner().invoke(app, ["import-csv", "NOBODY", str(SAMPLE), str(SAMPLE / "mapping.json"),
                                 "--db", str(db)])
    assert r.exit_code == 1


def test_phase_without_planned_length(cfg):
    # Not every plan has a set length (owner 2026-10-07): the phase still starts, week still counts.
    raw = json.loads((SAMPLE / "onboarding.json").read_text())
    raw["phase"].pop("planned_weeks")
    for p in raw.get("past_phases", []):
        p.pop("planned_weeks", None)
    from schemas.onboarding import OnboardingConfig
    oc = OnboardingConfig.model_validate(raw)
    store = EventStore()
    onboard(store, oc, cfg, base_dir=SAMPLE)
    phases = [e for e in store.read(CID) if e.type == "phase_started"]
    assert phases and all(e.payload.planned_weeks is None for e in phases)
    st = build_state(store.read(CID), oc.as_of, cfg)
    assert st.phase.planned_weeks is None and st.phase.week == oc.phase.current_phase_week
    from llm import prompt_builder
    prof = prompt_builder.user_profile(st)
    assert "planned_weeks" not in prof["phase"] and prof["phase"]["week"] == oc.phase.current_phase_week
