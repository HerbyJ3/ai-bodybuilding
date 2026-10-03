from datetime import date, timedelta

from engine import data_quality, proposals
from engine.data_quality import MesoSpan
from engine.state_builder import build_state
from schemas.proposals import new_proposal
from schemas.state import DataQualityFlag
from tests.conftest import ev, state, weigh_ins

AS_OF = date(2026, 3, 31)


def codes(events, mesos=None, cfg=None):
    return {f.code for f in data_quality.assess(events, AS_OF, cfg, mesos).flags}


def _set(d, muscle="chest", rir=2, sid=None):
    return ev("set_logged", d, {"exercise_id": "bench", "muscle": muscle, "load": 100, "reps": 10,
                                "rir": rir, "set_index": 0, "session_id": sid or f"s-{d}"})


def test_low_weigh_in_frequency(cfg):
    events = [ev("weigh_in", AS_OF - timedelta(days=i), {"weight": 200, "unit": "lb"}) for i in (0, 7, 9)]
    flags = data_quality.assess(events, AS_OF, cfg).flags
    low = [f for f in flags if f.code == "low_weigh_in_frequency"]
    assert len(low) == 1 and low[0].end == AS_OF  # only the latest window has 1


def test_no_weigh_ins_is_critical(cfg):
    [f] = [f for f in data_quality.assess([], AS_OF, cfg).flags if f.code == "no_weigh_ins"]
    assert f.severity == "critical"


def test_low_intake_logging(cfg):
    events = [ev("intake_logged", AS_OF - timedelta(days=i),
                 {"date": AS_OF - timedelta(days=i), "calories": 2000, "protein_g": 1, "carb_g": 1, "fat_g": 1})
              for i in (0, 1, 2, 3)]
    assert "low_intake_logging" in codes(events, cfg=cfg)


def test_stream_gap_and_stale_stream(cfg):
    events = [ev("weigh_in", d, {"weight": 200, "unit": "lb"})
              for d in (AS_OF - timedelta(days=30), AS_OF - timedelta(days=20), AS_OF - timedelta(days=10))]
    gaps = [f for f in data_quality.assess(events, AS_OF, cfg).flags if f.code == "stream_gap"]
    assert [(f.start, f.end) for f in gaps] == [
        (AS_OF - timedelta(days=30), AS_OF - timedelta(days=20)),
        (AS_OF - timedelta(days=20), AS_OF - timedelta(days=10)),
        (AS_OF - timedelta(days=10), AS_OF)]
    assert gaps[-1].severity == "critical"


def test_implausible_weight_excluded_from_trend(cfg):
    events = weigh_ins(AS_OF - timedelta(days=20), AS_OF, lambda d: 200)
    typo = ev("weigh_in", AS_OF - timedelta(days=1), {"weight": 120, "unit": "lb"})
    res = data_quality.assess(events + [typo], AS_OF, cfg)
    assert "implausible_weight" in res.codes() and typo.event_id in res.excluded_event_ids
    st = build_state(events + [typo], AS_OF, cfg)
    assert st.weight.latest_avg_lb == 200


def test_implausible_rir(cfg):
    assert "implausible_rir" in codes([_set(AS_OF, rir=12)], cfg=cfg)


def test_missing_soreness_skips_latest_window(cfg):
    assert "missing_soreness" not in codes([_set(AS_OF)], cfg=cfg)
    assert "missing_soreness" in codes([_set(AS_OF - timedelta(days=8))], cfg=cfg)
    sore = ev("soreness_rated", AS_OF - timedelta(days=3), {"muscle": "chest", "soreness": 1})
    assert "missing_soreness" not in codes([_set(AS_OF - timedelta(days=8)), sore], cfg=cfg)


def test_missing_stimulus_in_meso_week_1(cfg):
    start = AS_OF - timedelta(days=10)
    span = [MesoSpan("m", start, AS_OF + timedelta(days=1), 5)]
    assert "missing_stimulus" in codes([_set(start)], span, cfg)
    stim = ev("stimulus_rated", start, {"session_id": "x", "muscle": "chest", "mind_muscle": 1,
                                        "pump": 1, "disruption": 1})
    assert "missing_stimulus" not in codes([_set(start), stim], span, cfg)


def _flag(code, stream, severity="warn", days_ago=1):
    d = AS_OF - timedelta(days=days_ago)
    return DataQualityFlag(code=code, stream=stream, start=d, end=d, detail=code, severity=severity)


def test_confidence_from_relevant_recent_flags(cfg):
    p = new_proposal("c", AS_OF, "nutrition.weekly_adjustment", "calories.cut", "decrease_calories", "x")
    assert proposals.score(p, state(data_quality=[]), cfg).confidence == "high"
    assert proposals.score(p, state(data_quality=[_flag("a", "intake")]), cfg).confidence == "medium"
    two = [_flag("a", "intake"), _flag("b", "weigh_in")]
    assert proposals.score(p, state(data_quality=two), cfg).confidence == "low"
    crit = [_flag("a", "weigh_in", "critical")]
    assert proposals.score(p, state(data_quality=crit), cfg).confidence == "low"
    old = [_flag("a", "intake", days_ago=60), _flag("b", "weigh_in", days_ago=60)]
    assert proposals.score(p, state(data_quality=old), cfg).confidence == "high"
    other = [_flag("a", "soreness"), _flag("b", "sets")]
    assert proposals.score(p, state(data_quality=other), cfg).confidence == "high"


def test_low_confidence_can_only_hold_or_flag(cfg):
    for action, expected in [("add_sets", "hold"), ("decrease_calories", "hold"),
                             ("increase_calories", "flag"), ("recovery", "flag"),
                             ("hold", "hold"), ("flag", "flag")]:
        p = new_proposal("c", AS_OF, "r", "t", action, "x").model_copy(update={"confidence": "low"})
        assert proposals.enforce_low_confidence(p).action == expected
    p = new_proposal("c", AS_OF, "r", "t", "add_sets", "x").model_copy(update={"confidence": "medium"})
    assert proposals.enforce_low_confidence(p).action == "add_sets"


def test_one_weigh_in_this_week_low_confidence_hold(cfg):
    """BUILD_SPEC §11: one weigh-in only this week -> low confidence, hold."""
    events = [ev("phase_started", AS_OF - timedelta(days=60),
                 {"phase": "cut", "target_rate_pct_bw": 0.75, "planned_weeks": 12}, "coach")]
    events += weigh_ins(AS_OF - timedelta(days=40), AS_OF - timedelta(days=7), lambda d: 200)
    events.append(ev("weigh_in", AS_OF, {"weight": 199.8, "unit": "lb"}))
    st = build_state(events, AS_OF, cfg)
    nut = [p for p in proposals.generate(st, cfg) if p.rule_id.startswith("nutrition")]
    assert [(p.action, p.confidence) for p in nut] == [("hold", "low")]
