from datetime import date, timedelta

import pytest

from engine.training import fatigue, mev_estimator, set_progression
from engine.training.mesocycle import rir_target, validate_meso_length
from engine.training.outcome import Outcome, parse
from engine.training.performance import muscle_score, set_score
from schemas.events import SetLogged
from schemas.state import ClientState, MesoState, MuscleWeek
from tests.conftest import CID

# Book table 2.3 (soreness rows x performance cols), written out independently of the JSON.
EXPECTED = {
    (0, 0): Outcome("add_sets", 1, 3), (0, 1): Outcome("add_sets", 0, 2), (0, 2): Outcome("hold"), (0, 3): Outcome("recovery"),
    (1, 0): Outcome("add_sets", 1, 2), (1, 1): Outcome("add_sets", 0, 1), (1, 2): Outcome("hold"), (1, 3): Outcome("recovery"),
    (2, 0): Outcome("hold"), (2, 1): Outcome("hold"), (2, 2): Outcome("hold"), (2, 3): Outcome("recovery"),
    (3, 0): Outcome("hold"), (3, 1): Outcome("hold"), (3, 2): Outcome("hold"), (3, 3): Outcome("recovery"),
}


@pytest.mark.parametrize("cell", sorted(EXPECTED))
def test_every_matrix_cell(cfg, cell):
    assert set_progression.lookup(*cell, cfg) == EXPECTED[cell]


@pytest.mark.parametrize("perf", [2, 3])
@pytest.mark.parametrize("soreness", [0, 1, 2, 3])
def test_override_performance_ge_2_never_adds(cfg, soreness, perf):
    assert set_progression.lookup(soreness, perf, cfg).kind != "add_sets"


def test_override_enforced_even_if_json_says_add(cfg):
    import copy
    from config.loader import Config
    doc = copy.deepcopy(cfg.training_doc)
    doc["set_progression"]["matrix"]["0"][2] = "add 1-2"
    bad = Config(doc, cfg.nutrition_doc, cfg.settings_doc)
    assert set_progression.lookup(0, 2, bad).kind == "hold"


def test_lookup_rejects_out_of_range(cfg):
    with pytest.raises(ValueError):
        set_progression.lookup(4, 0, cfg)


def test_week1_performance_scored_1_to_2_conservatively(cfg):
    # soreness 0: perf 1 -> add 0-2, perf 2 -> hold; conservative merge -> hold
    assert set_progression.decide(0, None, None, None, cfg).kind == "hold"


def test_cut_caps_additions_for_non_beginners(cfg):
    assert set_progression.decide(0, 0, "cut", "intermediate", cfg) == Outcome("add_sets", 1, 1)
    assert set_progression.decide(0, 0, "cut", None, cfg) == Outcome("add_sets", 1, 1)
    assert set_progression.decide(0, 0, "cut", "beginner", cfg) == Outcome("add_sets", 1, 3)
    assert set_progression.decide(0, 0, "gain", "advanced", cfg) == Outcome("add_sets", 1, 3)


@pytest.mark.parametrize("total,kind", [(0, "add_sets"), (1, "add_sets"), (2, "add_sets"), (3, "add_sets"),
                                        (4, "defer"), (5, "defer"), (6, "defer"),
                                        (7, "reduce_sets"), (8, "reduce_sets"), (9, "reduce_sets")])
def test_mev_bands(cfg, total, kind):
    outcome, _ = mev_estimator.band_outcome(total, cfg)
    assert outcome.kind == kind
    if kind == "add_sets":
        assert (outcome.add_min, outcome.add_max) == (2, 4)


def test_parse_rejects_unknown_action():
    from config.loader import ConfigError
    with pytest.raises(ConfigError):
        parse("double it")


@pytest.mark.parametrize("weeks,expected", [
    (5, [(4, 5), (3, 4), (2, 3), (1, 2), (0, 1), None]),
    (4, [(4, 5), (3, 4), (1, 2), (0, 1), None]),
    (1, [(0, 1), None]),
])
def test_rir_schedule(cfg, weeks, expected):
    assert [rir_target(w, weeks, cfg) for w in range(1, weeks + 2)] == expected


def test_meso_length_warning(cfg):
    assert validate_meso_length(3, cfg) and not validate_meso_length(4, cfg)


def _s(load, reps, rir, idx=0):
    return SetLogged(exercise_id="e", muscle="m", load=load, reps=reps, rir=rir, set_index=idx, session_id="s")


@pytest.mark.parametrize("cur,score", [
    (_s(100, 12, 3), 0),   # +2 reps, RIR on target
    (_s(100, 11, 3), 1),   # +1 rep
    (_s(100, 10, 3), 1),   # matched
    (_s(100, 11, 1), 2),   # needed to go past target RIR
    (_s(100, 9, 3), 3),    # fewer reps
    (_s(95, 12, 3), 3),    # load dropped
])
def test_performance_set_score(cur, score):
    assert set_score(cur, _s(100, 10, 3), (2, 3)) == score


def test_performance_averaged_and_week1_none():
    cur = {"e": [_s(100, 12, 3, 0), _s(100, 11, 3, 1)]}  # 0 and 1 -> 0.5 -> rounds to 1
    prev = {"e": [_s(100, 10, 3, 0), _s(100, 10, 3, 1)]}
    assert muscle_score(cur, prev, (2, 3)) == 1
    assert muscle_score(cur, {}, (2, 3)) is None
    assert muscle_score(cur, prev, None) is None


def _meso_state(week=3, weeks_planned=5, muscles=None, last_deload=None):
    start = date(2026, 3, 31) - timedelta(days=7 * (week - 1))
    st = ClientState(client_id=CID, as_of=date(2026, 3, 31), last_deload_end=last_deload)
    st.meso = MesoState(meso_id="m", start_date=start, week=week, weeks_planned=weeks_planned,
                        rir_target=rir_target(week, weeks_planned, __import__("config").load_config()),
                        is_deload_week=week > weeks_planned, exercises_per_muscle={},
                        starting_sets_per_muscle={}, source="coach")
    st.muscle_weeks = muscles or {}
    return st


def _mw(week, soreness=None, perf=None, stim=None, sets=6):
    return MuscleWeek(meso_id="m", meso_week=week, week_start=date(2026, 3, 17) + timedelta(days=7 * (week - 1)),
                      sets=sets, soreness=soreness, performance=perf, stimulus_total=stim)


def test_set_progression_proposals(cfg):
    st = _meso_state(muscles={"chest": [_mw(1), _mw(2, 0, 0)], "back": [_mw(2, 1, 3)],
                              "quads": [_mw(1, 0, None)]})  # quads: stale (week 1 < current-1)
    props = {p.target: p for p in set_progression.evaluate(st, cfg)}
    assert props["volume.chest"].action == "add_sets"
    assert props["volume.chest"].proposed_value == {"add_sets_min": 1, "add_sets_max": 3}
    assert props["volume.back"].action == "recovery"
    assert "volume.quads" not in props
    assert props["volume.chest"].config_keys[0] == "set_progression.matrix"


def test_mev_estimator_proposals(cfg):
    st = _meso_state(week=2, muscles={"chest": [_mw(1, stim=2)], "back": [_mw(1, stim=5)],
                                      "quads": [_mw(1, stim=8)]})
    props = {p.target: p.action for p in mev_estimator.evaluate(st, cfg)}
    assert props == {"volume.chest": "add_sets", "volume.quads": "reduce_sets"}
    assert mev_estimator.evaluate(_meso_state(week=3, muscles=st.muscle_weeks), cfg) == []


def test_deload_on_final_week(cfg):
    [p] = fatigue.evaluate(_meso_state(week=5), cfg, [])
    assert p.action == "deload"
    assert p.proposed_value["first_half"] == "same loads, half the sets and reps"
    assert p.proposed_value["second_half"] == "half the sets, reps, and load"


def test_deload_past_plan_unless_done(cfg):
    assert fatigue.evaluate(_meso_state(week=6), cfg, [])
    st = _meso_state(week=6, last_deload=date(2026, 3, 30))
    assert fatigue.evaluate(st, cfg, []) == []


def test_deload_when_multiple_muscles_need_recovery(cfg):
    assert fatigue.evaluate(_meso_state(week=3), cfg, ["chest"]) == []
    assert fatigue.evaluate(_meso_state(week=3), cfg, ["chest", "back"])[0].action == "deload"
    assert fatigue.evaluate(_meso_state(week=3), cfg, ["chest", "back"],
                            include_performance_trigger=False) == []
