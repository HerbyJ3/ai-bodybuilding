"""Phase duration limits and transitions (nutrition-defaults.json `phases`, `transitions`)."""
from __future__ import annotations

from config.loader import Config
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState

RULE_ID = "nutrition.phase_transition"


def _latest_performance_mean(state: ClientState) -> float | None:
    scores = [ws[-1].performance for ws in state.muscle_weeks.values()
              if ws and ws[-1].performance is not None]
    return sum(scores) / len(scores) if scores else None


def evaluate(state: ClientState, cfg: Config) -> list[Proposal]:
    ph = state.phase
    if ph is None:
        return []
    cid, as_of = state.client_id, state.as_of
    reasons, keys = [], []
    if ph.phase == "cut":
        dur = cfg.nutrition("phases.cut.duration_weeks")
        keys = ["phases.cut.duration_weeks"]
        hunger = state.latest_checkin["hunger"] if state.latest_checkin else None
        perf = _latest_performance_mean(state)
        hunger_hi = hunger is not None and hunger >= cfg.setting("phase_transition.hunger_threshold")
        perf_fall = perf is not None and perf >= cfg.setting("phase_transition.performance_falling_score")
        if ph.week > dur["max"]:
            reasons.append(f"cut week {ph.week} > max {dur['max']}")
        elif ph.week > dur["recommended"][1] and (hunger_hi or perf_fall):
            reasons.append(f"cut week {ph.week} > recommended {dur['recommended'][1]} with "
                           + ", ".join(s for s, on in (("high hunger", hunger_hi),
                                                       ("falling performance", perf_fall)) if on))
        if not reasons:
            return []
        proposed: dict = {"phase": "maintenance",
                          "procedure": cfg.nutrition("transitions.cut_to_maintenance")}
        keys.append("transitions.cut_to_maintenance")
        flags = []
        mt, intake = state.maintenance, state.avg_intake_kcal
        if mt and mt.confidence in ("high", "medium") and intake is not None:
            proposed["calories"] = round((intake + mt.kcal) / 2)
        else:
            flags.append("no_maintenance_estimate")
        return [new_proposal(cid, as_of, RULE_ID, "phase", "transition_phase",
                             rationale="; ".join(reasons), current_value="cut",
                             proposed_value=proposed, config_keys=keys,
                             data_quality_flags=flags,
                             inputs_used={"phase_week": ph.week, "hunger": hunger,
                                          "performance_mean": perf, "avg_intake_kcal": intake,
                                          "maintenance_kcal": mt.kcal if mt else None})]
    if ph.phase == "gain":
        age = state.training_age
        max_key = "max_beginner" if age == "beginner" else "max_advanced"
        limit = cfg.nutrition(f"phases.gain.duration_weeks.{max_key}")
        if ph.week <= limit:
            return []
        proposed = {"phase": "maintenance", "procedure": cfg.nutrition("transitions.gain_to_maintenance")}
        flags = []
        t = state.weight
        if state.avg_intake_kcal is not None and t.avg_weekly_change_lb is not None:
            surplus = max(0.0, t.avg_weekly_change_lb) * cfg.nutrition("tracking.kcal_per_lb_tissue") / 7
            proposed["calories"] = round(state.avg_intake_kcal - surplus)
        else:
            flags.append("no_intake_or_trend")
        return [new_proposal(cid, as_of, RULE_ID, "phase", "transition_phase",
                             rationale=f"gain week {ph.week} > max {limit} ({max_key})",
                             current_value="gain", proposed_value=proposed,
                             data_quality_flags=flags,
                             inputs_used={"phase_week": ph.week, "training_age": age},
                             config_keys=[f"phases.gain.duration_weeks.{max_key}",
                                          "transitions.gain_to_maintenance"])]
    return []
