"""Weekly calorie adjustment (BUILD_SPEC §7.2)."""
from __future__ import annotations

from datetime import timedelta

from config.loader import Config
from engine import cardio_lever
from engine.nutrition import macros as macro_rules
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState

RULE_ID = "nutrition.weekly_adjustment"


def kcal_per_day_for_rate_gap(gap_pct_bw: float, bw_lb: float, cfg: Config) -> float:
    """kcal/day = (lb/week gap × kcal_per_lb) / 7"""
    lb_per_week = gap_pct_bw / 100 * bw_lb
    return lb_per_week * cfg.nutrition("tracking.kcal_per_lb_tissue") / 7


def carbs_below_minimum(state: ClientState, bw: float, cfg: Config) -> list[str]:
    """Flags day types whose current carbs already sit below that day type's minimum:
    usually a sign the client's day was mapped to the wrong day type."""
    out = []
    for day_type, m in (state.current_macros or {}).items():
        if m.carb_g < cfg.nutrition(f"carbs.by_day_type.{day_type}.minimum") * bw:
            out.append(f"carbs_below_day_type_minimum:{day_type}")
    return out


def _rate_band(phase: str, cfg: Config) -> tuple[float, float] | None:
    if phase in ("cut", "gain"):
        lo, hi = cfg.nutrition(f"phases.{phase}.rate_pct_bw_per_week")
        return lo, hi
    return None


def evaluate(state: ClientState, cfg: Config) -> list[Proposal]:
    ph = state.phase
    if ph is None:
        return []
    cid, as_of = state.client_id, state.as_of
    base_keys = ["tracking.min_weeks_before_trend", "tracking.kcal_per_lb_tissue"]

    if ph.phase in ("recomp", "mini_cut"):
        return [new_proposal(cid, as_of, RULE_ID, f"calories.{ph.phase}", "flag",
                             rationale=f"{ph.phase}: no tabulated rate rule; coach review",
                             inputs_used={"phase": ph.phase},
                             config_keys=[f"phases.{ph.phase}"] if ph.phase == "mini_cut" else [])]

    checkin = state.latest_checkin
    threshold = cfg.setting("adherence_threshold_pct")
    max_age = cfg.setting("adherence.checkin_max_age_days")
    # Minimum data: weigh-ins (trend check below) + macro targets. Check-ins are optional:
    # without a recent one we proceed, and scoring caps confidence at medium.
    recent_checkin = checkin is not None and (as_of - checkin["date"]).days <= max_age
    optional_flags = [] if recent_checkin else ["no_recent_checkin"]
    if recent_checkin and checkin["adherence_pct"] < threshold:
        return [new_proposal(cid, as_of, RULE_ID, f"calories.{ph.phase}", "adherence_intervention",
                             rationale=f"adherence {checkin['adherence_pct']:.0f}% < {threshold}%: "
                                       "fix adherence before changing calories",
                             inputs_used={"adherence_pct": checkin["adherence_pct"]},
                             config_keys=["engine-settings:adherence_threshold_pct"])]
    if not state.current_macros:
        return [new_proposal(cid, as_of, RULE_ID, f"calories.{ph.phase}", "hold",
                             rationale="no macro targets on record: set them to enable calorie adjustments",
                             config_keys=[], data_quality_flags=["no_current_targets"])]

    t = state.weight
    min_weeks = cfg.nutrition("tracking.min_weeks_before_trend")
    if t.trend_weeks < min_weeks or t.pct_bw_per_week is None:
        return [new_proposal(cid, as_of, RULE_ID, f"calories.{ph.phase}", "hold",
                             rationale=f"{t.trend_weeks} week(s) of trend data; need {min_weeks}",
                             inputs_used={"trend_weeks": t.trend_weeks}, config_keys=base_keys)]

    bw = t.latest_avg_lb
    rate = t.pct_bw_per_week  # signed, + = gaining
    target = ph.target_rate_pct_bw
    inputs = {"phase": ph.phase, "pct_bw_per_week": round(rate, 3), "target_rate_pct_bw": target,
              "bodyweight_lb": round(bw, 1), "trend_weeks": t.trend_weeks,
              "adherence_pct": checkin["adherence_pct"] if recent_checkin else None}

    if ph.phase == "maintenance":
        band = cfg.nutrition("phases.maintenance.stable_band_pct_bw")
        span = min(t.trend_weeks, cfg.nutrition("tracking.assess_window_weeks")[1]) - 1
        drift = rate * span
        keys = base_keys + ["phases.maintenance.stable_band_pct_bw"]
        if abs(drift) <= band:
            return [new_proposal(cid, as_of, RULE_ID, "calories.maintenance", "hold",
                                 rationale=f"weight drift {drift:+.2f}% within ±{band}% band",
                                 inputs_used=inputs, config_keys=keys)]
        gap = -rate  # bring the rate back to 0
    else:
        lo, hi = _rate_band(ph.phase, cfg)  # type: ignore[misc]
        keys = base_keys + [f"phases.{ph.phase}.rate_pct_bw_per_week"]
        directional = -rate if ph.phase == "cut" else rate  # loss rate for cut, gain rate for gain
        if lo <= directional <= hi:
            return [new_proposal(cid, as_of, RULE_ID, f"calories.{ph.phase}", "hold",
                                 rationale=f"{ph.phase} rate {directional:.2f}%/wk within {lo}-{hi}%",
                                 inputs_used=inputs, config_keys=keys)]
        # calorie change that moves the actual rate to the target
        gap = (directional - target) if ph.phase == "cut" else (target - directional)

    full = round(kcal_per_day_for_rate_gap(gap, bw, cfg))
    cap = cfg.setting("calorie_step_cap.max_kcal_change_per_step")
    kcal = max(-cap, min(cap, full))
    inputs["full_gap_kcal_per_day"] = full
    keys = keys + ["engine-settings:calorie_step_cap"]
    flags: list[str] = (["step_capped"] if kcal != full else []) + optional_flags
    flags += carbs_below_minimum(state, bw, cfg)

    # In a cut, cheaper reversible cardio levers come before calories.
    if ph.phase in ("cut", "mini_cut") and kcal < 0:
        step, considered = cardio_lever.next_step(state, cfg)
        inputs["cardio_levers_considered"] = considered
        if step is not None:
            return [new_proposal(
                cid, as_of, "nutrition.cardio_lever", "cardio", "increase_cardio",
                rationale=f"{ph.phase}: {rate:+.2f}%/wk vs target {target}%/wk -> cardio {step.lever} "
                          f"first (~{step.kcal_per_day} kcal/day, ~{step.lb_per_week} lb/week); "
                          "calories unchanged",
                current_value=step.current, proposed_value={"lever": step.lever, **step.proposed,
                                                            "est_kcal_per_day": step.kcal_per_day,
                                                            "est_lb_per_week": step.lb_per_week},
                inputs_used=inputs, config_keys=keys + ["engine-settings:cardio_lever"],
                data_quality_flags=sorted(set(flags)))]

    action = "increase_calories" if kcal > 0 else "decrease_calories"
    proposed: dict = {"kcal_per_day_change": kcal}
    new = {}
    for day_type, m in state.current_macros.items():
        r = macro_rules.apply_change(m, kcal, bw, day_type, cfg)
        new[day_type] = r.macros.model_dump()
        flags += r.flags
    proposed["macros_by_day_type"] = new
    keys += ["phases.cut.macro_cut_order", "phases.gain.macro_add_order", "fat.minimum"]
    return [new_proposal(
        cid, as_of, RULE_ID, f"calories.{ph.phase}", action,
        rationale=f"{ph.phase}: {rate:+.2f}%/wk vs target {target}%/wk -> {kcal:+d} kcal/day"
                  + (f" (capped; full gap {full:+d})" if kcal != full else ""),
        current_value={k: v.model_dump() for k, v in (state.current_macros or {}).items()} or None,
        proposed_value=proposed, inputs_used=inputs, config_keys=keys,
        data_quality_flags=sorted(set(flags)))]
