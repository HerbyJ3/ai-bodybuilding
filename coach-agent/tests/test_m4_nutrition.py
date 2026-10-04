from datetime import date

import pytest

from engine import cardio
from engine.nutrition import adjustment, macros, phases
from engine.nutrition.maintenance import MaintenanceFormulaUndecided, calibrate, initial_estimate
from schemas.events import MacroTargets
from schemas.state import MaintenanceEstimate, MuscleWeek
from tests.conftest import state, trend

MT = MacroTargets


# --- macros -----------------------------------------------------------------

def test_macros_fat_is_remainder(cfg):
    r = macros.compute(200, 3000, "cut", "moderate", cfg)
    assert r.macros == MT(protein_g=200, carb_g=300, fat_g=111) and r.flags == []


def test_macros_fat_floor_moves_kcal_from_carbs(cfg):
    r = macros.compute(200, 2500, "cut", "moderate", cfg)
    assert r.macros == MT(protein_g=200, carb_g=290, fat_g=60)


def test_macros_flag_when_carbs_below_day_minimum(cfg):
    r = macros.compute(200, 1500, "cut", "moderate", cfg)
    assert r.macros.fat_g == 60 and "carbs_below_day_type_minimum" in r.flags


@pytest.mark.parametrize("phase,g_per_lb", [("cut", 1.0), ("gain", 1.0), ("maintenance", 1.0), (None, 1.0)])
def test_protein_by_phase(cfg, phase, g_per_lb):
    assert macros.protein_g_per_lb(phase, cfg) == g_per_lb


def test_decrease_takes_fat_then_carbs_never_protein(cfg):
    r = macros.apply_change(MT(protein_g=200, carb_g=300, fat_g=80), -300, 200, "moderate", cfg)
    assert r.macros == MT(protein_g=200, carb_g=270, fat_g=60)


def test_decrease_with_fat_at_floor_uses_carbs(cfg):
    r = macros.apply_change(MT(protein_g=200, carb_g=300, fat_g=60), -200, 200, "moderate", cfg)
    assert r.macros == MT(protein_g=200, carb_g=250, fat_g=60)


def test_decrease_flags_when_floors_reached(cfg):
    r = macros.apply_change(MT(protein_g=200, carb_g=210, fat_g=60), -200, 200, "moderate", cfg)
    assert r.macros == MT(protein_g=200, carb_g=200, fat_g=60)
    assert "floors_reached_change_not_fully_applied" in r.flags


def test_increase_adds_carbs(cfg):
    r = macros.apply_change(MT(protein_g=200, carb_g=300, fat_g=60), 200, 200, "moderate", cfg)
    assert r.macros == MT(protein_g=200, carb_g=350, fat_g=60)


# --- weekly adjustment (BUILD_SPEC §11 engine scenarios) ---------------------

def test_kcal_formula(cfg):
    # 0.5 %BW of 200 lb = 1 lb/week -> 500 kcal/day
    assert adjustment.kcal_per_day_for_rate_gap(0.5, 200, cfg) == pytest.approx(500)


def test_cut_low_adherence_intervention_not_calorie_change(cfg):
    [p] = adjustment.evaluate(state("cut", pct=0.0, adherence=70), cfg)
    assert p.action == "adherence_intervention" and p.proposed_value is None


def test_cut_slow_loss_reduces_fat_then_carbs(cfg):
    st = state("cut", pct=-0.2, current_macros={"moderate": MT(protein_g=200, carb_g=300, fat_g=80)})
    [p] = adjustment.evaluate(st, cfg)
    # gap 0.2 - 0.75 = -0.55 %BW = -1.1 lb/wk = -550 kcal/day, capped at 250 per step
    assert p.action == "decrease_calories"
    assert p.inputs_used["full_gap_kcal_per_day"] == -550
    assert p.proposed_value["kcal_per_day_change"] == -250 and "step_capped" in p.data_quality_flags
    # fat 80 -> floor 60 (180 kcal), remaining 70 kcal from carbs
    assert p.proposed_value["macros_by_day_type"]["moderate"] == {"protein_g": 200, "carb_g": 282, "fat_g": 60}


def test_cut_fat_at_floor_reduces_carbs(cfg):
    st = state("cut", pct=-0.2, current_macros={"moderate": MT(protein_g=200, carb_g=400, fat_g=60)})
    [p] = adjustment.evaluate(st, cfg)
    assert p.proposed_value["macros_by_day_type"]["moderate"] == {"protein_g": 200, "carb_g": 338, "fat_g": 60}


def test_cut_in_band_holds(cfg):
    [p] = adjustment.evaluate(state("cut", pct=-0.7), cfg)
    assert p.action == "hold"


def test_cut_too_fast_increases(cfg):
    [p] = adjustment.evaluate(state("cut", pct=-1.25), cfg)
    assert p.action == "increase_calories" and p.proposed_value["kcal_per_day_change"] == 250
    assert p.inputs_used["full_gap_kcal_per_day"] == 500


def test_gain_too_fast_reduces_surplus(cfg):
    [p] = adjustment.evaluate(state("gain", target=0.4, pct=1.0), cfg)
    assert p.action == "decrease_calories" and p.proposed_value["kcal_per_day_change"] == -250
    assert p.inputs_used["full_gap_kcal_per_day"] == -600


def test_gain_too_slow_adds(cfg):
    [p] = adjustment.evaluate(state("gain", target=0.4, pct=0.1), cfg)
    assert p.action == "increase_calories" and p.proposed_value["kcal_per_day_change"] == 250
    assert p.inputs_used["full_gap_kcal_per_day"] == 300


def test_needs_two_weeks_of_trend(cfg):
    st = state("cut", pct=-0.2)
    st.weight = trend(-0.2, weeks=1)
    st.weight.pct_bw_per_week = None
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "hold"


def test_no_current_targets_flagged(cfg):
    [p] = adjustment.evaluate(state("cut", pct=-0.2), cfg)
    assert "no_current_targets" in p.data_quality_flags and "macros_by_day_type" not in p.proposed_value


def test_maintenance_band(cfg):
    assert adjustment.evaluate(state("maintenance", target=0, pct=0.3), cfg)[0].action == "hold"
    [p] = adjustment.evaluate(state("maintenance", target=0, pct=1.0), cfg)
    assert p.action == "decrease_calories" and p.proposed_value["kcal_per_day_change"] == -250


@pytest.mark.parametrize("phase", ["recomp", "mini_cut"])
def test_untabulated_phases_flag_only(cfg, phase):
    assert adjustment.evaluate(state(phase, pct=-0.2), cfg)[0].action == "flag"


# --- maintenance calibration -------------------------------------------------

def test_calibration_needs_three_weeks(cfg):
    assert calibrate(trend(-0.5, weeks=2), [(date(2026, 3, 30), 2500)], date(2026, 3, 31), cfg) is None


def test_calibration_confidence_drops_with_sparse_intake(cfg):
    as_of = date(2026, 3, 31)
    intake = [(date(2026, 3, d), 2500) for d in range(11, 32) if d % 7 in (0, 1, 2, 3)]  # 3/wk
    m = calibrate(trend(-0.5), intake, as_of, cfg)
    assert m.kcal == 3000 and m.confidence == "low"


def test_initial_estimate_is_undecided():
    with pytest.raises(MaintenanceFormulaUndecided):
        initial_estimate(200)


# --- phases --------------------------------------------------------------------

def test_cut_week_14_hunger_5_transitions(cfg):
    st = state("cut", week=14, pct=-0.5, hunger=5, avg_intake_kcal=2000,
               maintenance=MaintenanceEstimate(kcal=2800, confidence="medium", method="calibrated", weeks_used=3))
    [p] = phases.evaluate(st, cfg)
    assert p.action == "transition_phase" and p.proposed_value["phase"] == "maintenance"
    assert p.proposed_value["calories"] == 2400  # midpoint of intake and maintenance


def test_cut_week_14_falling_performance_transitions(cfg):
    st = state("cut", week=14, hunger=2, muscle_weeks={
        "chest": [MuscleWeek(meso_id="m", meso_week=3, week_start=date(2026, 3, 24), performance=3)]})
    [p] = phases.evaluate(st, cfg)
    assert "falling performance" in p.rationale_short
    assert "no_maintenance_estimate" in p.data_quality_flags


def test_cut_week_14_without_fatigue_continues(cfg):
    assert phases.evaluate(state("cut", week=14, hunger=2), cfg) == []


def test_cut_past_max_transitions(cfg):
    assert phases.evaluate(state("cut", week=17, hunger=1), cfg)[0].action == "transition_phase"


def test_gain_duration_by_training_age(cfg):
    assert phases.evaluate(state("gain", week=17, training_age="advanced"), cfg)
    assert phases.evaluate(state("gain", week=17, training_age="beginner"), cfg) == []


# --- cardio (provisional) ------------------------------------------------------

@pytest.mark.parametrize("sets,kcal,phase,expected", [
    (0, 0, "cut", "non_training"), (6, 0, "cut", "light"), (10, 0, "cut", "light"),
    (10, 0, "gain", "moderate"), (18, 0, "cut", "moderate"), (25, 0, "cut", "moderate"),
    (25, 0, "gain", "hard"), (30, 0, "cut", "hard"),
    (0, 300, "cut", "light"), (0, 500, "cut", "moderate"), (6, 240, "cut", "moderate"),
])
def test_day_classification(cfg, sets, kcal, phase, expected):
    assert cardio.classify_day(sets, kcal, phase, cfg) == expected


def test_cardio_kcal_estimate(cfg):
    assert cardio.cardio_kcal(30, "low", None, cfg) == 150
    assert cardio.cardio_kcal(30, "low", 210, cfg) == 210


def test_cardio_interference_flags_only(cfg):
    st = state("cut", cardio_minutes_by_week=[120, 60], muscle_weeks={
        "quads": [MuscleWeek(meso_id="m", meso_week=2, week_start=date(2026, 3, 17), soreness=1),
                  MuscleWeek(meso_id="m", meso_week=3, week_start=date(2026, 3, 24), soreness=3)]})
    [p] = cardio.interference(st)
    assert p.action == "flag"
    st.cardio_minutes_by_week = [60, 120]
    assert cardio.interference(st) == []


# --- owner decisions 2026-10-04 -----------------------------------------------------

def test_small_gap_not_capped(cfg):
    # losing 0.4 %/wk vs 0.6 target: gap 0.2 %BW = 0.4 lb/wk = 200 kcal/day, under the cap
    [p] = adjustment.evaluate(state("cut", target=0.6, pct=-0.4), cfg)
    assert p.proposed_value["kcal_per_day_change"] == p.inputs_used["full_gap_kcal_per_day"] == -200
    assert "step_capped" not in p.data_quality_flags


def test_no_checkin_holds_calories(cfg):
    [p] = adjustment.evaluate(state("cut", pct=-0.2, adherence=None), cfg)
    assert p.action == "hold" and "no_recent_checkin" in p.data_quality_flags


def test_stale_checkin_holds_calories(cfg):
    from datetime import timedelta
    st = state("cut", pct=-0.2)
    st.latest_checkin["date"] = st.as_of - timedelta(days=15)
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "hold" and "last" in p.rationale_short


def test_carbs_below_day_type_minimum_flagged(cfg):
    # 190 g on a 'moderate' day at 207 lb is below 1.0 g/lb; as 'light' (0.5 g/lb) it is fine
    st = state("cut", pct=-0.2, current_macros={"moderate": MT(protein_g=220, carb_g=150, fat_g=65)})
    [p] = adjustment.evaluate(st, cfg)
    assert "carbs_below_day_type_minimum:moderate" in p.data_quality_flags
    st = state("cut", pct=-0.2, current_macros={"light": MT(protein_g=220, carb_g=150, fat_g=65)})
    [p] = adjustment.evaluate(st, cfg)
    assert not [f for f in p.data_quality_flags if f.startswith("carbs_below")]


def _cardio(st, sessions, minutes, kcal):
    from datetime import timedelta
    st.cardio_recent = [{"date": st.as_of - timedelta(days=2 * i), "minutes": minutes, "intensity": "mod",
                         "modality": "incline walk", "est_kcal": kcal} for i in range(sessions)]
    return st


def test_cardio_frequency_before_calories(cfg):
    # 4 sessions in 14 days = 2/week; +1 session of 45 min at 10 kcal/min = 450 kcal/wk ~0.13 lb
    st = _cardio(state("cut", pct=-0.2), sessions=4, minutes=45, kcal=450)  # 10 kcal/min
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "increase_cardio" and p.rule_id == "nutrition.cardio_lever"
    assert p.proposed_value["lever"] == "frequency" and p.proposed_value["sessions_per_week"] == 3
    assert p.proposed_value["modality"] == "incline walk"  # limitation-safe: same modality
    assert p.proposed_value["est_lb_per_week"] == round(450 / 3500, 2)


def test_cardio_duration_when_frequency_at_ceiling(cfg):
    # 3/week at the ceiling; +10 min x 3 sessions at 14 kcal/min = 420 kcal/wk ~0.12 lb
    st = _cardio(state("cut", pct=-0.2, cardio_max_sessions_per_week=3), sessions=6, minutes=30, kcal=420)
    [p] = adjustment.evaluate(st, cfg)
    assert p.proposed_value["lever"] == "duration" and p.proposed_value["minutes_per_session"] == 40
    skipped = p.inputs_used["cardio_levers_considered"]
    assert skipped[0]["lever"] == "frequency" and "ceiling" in skipped[0]["skipped"]


def test_cardio_below_noise_floor_falls_through_to_calories(cfg):
    st = _cardio(state("cut", pct=-0.2, cardio_max_sessions_per_week=3), sessions=6, minutes=35, kcal=200)
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "decrease_calories"
    assert all("noise floor" in c["skipped"] or "ceiling" in c["skipped"]
               for c in p.inputs_used["cardio_levers_considered"])


def test_cardio_lever_only_in_cut(cfg):
    st = _cardio(state("gain", target=0.4, pct=1.0), sessions=2, minutes=45, kcal=450)
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "decrease_calories"


def test_no_cardio_history_goes_to_calories(cfg):
    [p] = adjustment.evaluate(state("cut", pct=-0.2), cfg)
    assert p.action == "decrease_calories"
    assert "no recent cardio" in p.inputs_used["cardio_levers_considered"][0]["skipped"]
