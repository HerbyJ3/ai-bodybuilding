"""Weekly calorie adjustment (BUILD_SPEC §7.2)."""
from __future__ import annotations

from config.loader import Config
from engine.nutrition import macros as macro_rules
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState

RULE_ID = "nutrition.weekly_adjustment"


def kcal_per_day_for_rate_gap(gap_pct_bw: float, bw_lb: float, cfg: Config) -> float:
    """kcal/day = (lb/week gap × kcal_per_lb) / 7"""
    lb_per_week = gap_pct_bw / 100 * bw_lb
    return lb_per_week * cfg.nutrition("tracking.kcal_per_lb_tissue") / 7


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
    if checkin and checkin["adherence_pct"] < threshold:
        return [new_proposal(cid, as_of, RULE_ID, f"calories.{ph.phase}", "adherence_intervention",
                             rationale=f"adherence {checkin['adherence_pct']:.0f}% < {threshold}%: "
                                       "fix adherence before changing calories",
                             inputs_used={"adherence_pct": checkin["adherence_pct"]},
                             config_keys=["engine-settings:adherence_threshold_pct"])]

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
              "adherence_pct": checkin["adherence_pct"] if checkin else None}

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

    kcal = round(kcal_per_day_for_rate_gap(gap, bw, cfg))
    action = "increase_calories" if kcal > 0 else "decrease_calories"
    proposed: dict = {"kcal_per_day_change": kcal}
    flags: list[str] = []
    if state.current_macros:
        new = {}
        for day_type, m in state.current_macros.items():
            r = macro_rules.apply_change(m, kcal, bw, day_type, cfg)
            new[day_type] = r.macros.model_dump()
            flags += r.flags
        proposed["macros_by_day_type"] = new
        keys += ["phases.cut.macro_cut_order", "phases.gain.macro_add_order", "fat.minimum"]
    else:
        flags.append("no_current_targets")
    return [new_proposal(
        cid, as_of, RULE_ID, f"calories.{ph.phase}", action,
        rationale=f"{ph.phase}: {rate:+.2f}%/wk vs target {target}%/wk -> {kcal:+d} kcal/day",
        current_value={k: v.model_dump() for k, v in (state.current_macros or {}).items()} or None,
        proposed_value=proposed, inputs_used=inputs, config_keys=keys,
        data_quality_flags=sorted(set(flags)))]
