"""Runs all rules, applies cold-start gates, scores confidence from data-quality
flags and enforces the low-confidence restriction (BUILD_SPEC §7.5)."""
from __future__ import annotations

from datetime import timedelta

from config.loader import Config
from engine import cardio, cold_start
from engine.nutrition import adjustment, phases
from engine.training import fatigue, mev_estimator, set_progression
from schemas.proposals import ESCALATING_ACTIONS, Proposal, new_proposal
from schemas.state import ClientState

RULE_STREAMS = {
    "training": {"sets", "soreness", "stimulus"},
    "nutrition": {"weigh_in", "intake", "checkin"},
    "cardio": {"sets", "soreness"},
}


def _muscle(p: Proposal) -> str | None:
    return p.target.split(".", 1)[1] if p.target.startswith("volume.") else None


def _cold_hold(p: Proposal, have: int, need: int) -> Proposal:
    flags = ["cold_start_training"]
    if p.action in ("recovery", "reduce_sets"):
        flags.append("fatigue_signal_during_cold_start")  # surfaced for coach review
    return p.model_copy(update={
        "action": "hold", "proposed_value": {"add_sets": 0},
        "inputs_used": {**p.inputs_used, "cold_start_suppressed_action": p.action,
                        "rated_weeks": have, "rated_weeks_needed": need},
        "config_keys": p.config_keys + ["engine-settings:cold_start"],
        "data_quality_flags": sorted(set(p.data_quality_flags) | set(flags)),
        "rationale_short": f"cold start: {have}/{need} rated weeks; holding ({p.rationale_short})",
    })


def training_proposals(state: ClientState, cfg: Config) -> list[Proposal]:
    gate = cold_start.training_status(state, cfg)
    mev = mev_estimator.evaluate(state, cfg)
    sp = set_progression.evaluate(state, cfg, skip_muscles={_muscle(p) for p in mev})
    out = []
    for p in mev + sp:
        g = gate.get(_muscle(p) or "")
        out.append(p if g is None or g.ready else _cold_hold(p, g.have_weeks, g.need_weeks))
    # Gated muscles with nothing to evaluate yet still get an explicit hold.
    covered = {_muscle(p) for p in out}
    if state.meso is not None:
        for muscle, g in gate.items():
            if not g.ready and muscle not in covered:
                out.append(new_proposal(
                    state.client_id, state.as_of, "training.cold_start", f"volume.{muscle}", "hold",
                    rationale=f"cold start: {g.have_weeks}/{g.need_weeks} weeks of soreness + "
                              f"performance for {muscle}; holding",
                    proposed_value={"add_sets": 0},
                    inputs_used={"rated_weeks": g.have_weeks, "rated_weeks_needed": g.need_weeks},
                    config_keys=["engine-settings:cold_start"],
                    data_quality_flags=["cold_start_training"]))
    recovery = [_muscle(p) for p in out if p.action == "recovery"]
    all_ready = all(g.ready for g in gate.values())
    out += fatigue.evaluate(state, cfg, recovery, include_performance_trigger=all_ready)
    return out


def nutrition_proposals(state: ClientState, cfg: Config) -> list[Proposal]:
    if state.phase is None:
        return []
    g = cold_start.nutrition_status(state, cfg)
    if not g.ready:
        return [new_proposal(
            state.client_id, state.as_of, "nutrition.cold_start", f"calories.{state.phase.phase}",
            "hold", rationale=f"cold start: {g.have_weeks}/{g.need_weeks} weeks of weigh-ins",
            inputs_used={"trend_weeks": g.have_weeks, "needed": g.need_weeks},
            config_keys=["tracking.min_weeks_before_trend", "tracking.weigh_ins_per_week"],
            data_quality_flags=["cold_start_nutrition"])]
    return adjustment.evaluate(state, cfg) + phases.evaluate(state, cfg)


def score(p: Proposal, state: ClientState, cfg: Config) -> Proposal:
    domain = p.rule_id.split(".", 1)[0]
    streams = RULE_STREAMS.get(domain, set())
    since = state.as_of - timedelta(days=cfg.setting("confidence.lookback_days"))
    muscle = _muscle(p)
    relevant = [f for f in state.data_quality
                if f.stream in streams and f.end >= since
                and (muscle is None or f.stream == "sets" or muscle in f.detail
                     or f.code == "stream_gap")]
    n = len(relevant)
    if n >= cfg.setting("confidence.low_at_flag_count") or any(f.severity == "critical" for f in relevant):
        conf = "low"
    elif n >= cfg.setting("confidence.medium_at_flag_count"):
        conf = "medium"
    else:
        conf = "high"
    # Optional data that is missing (e.g. no recent check-in) never blocks, but caps confidence.
    if domain == "nutrition" and "no_recent_checkin" in p.data_quality_flags and conf == "high":
        conf = "medium"
    flags = sorted(set(p.data_quality_flags) | {f.code for f in relevant})
    return p.model_copy(update={"confidence": conf, "data_quality_flags": flags})


def enforce_low_confidence(p: Proposal) -> Proposal:
    """Low confidence may only hold or flag; never add volume or deepen a deficit."""
    if p.confidence != "low" or p.action in ("hold", "flag"):
        return p
    new_action = "hold" if p.action in ESCALATING_ACTIONS else "flag"
    return p.model_copy(update={
        "action": new_action,
        "inputs_used": {**p.inputs_used, "low_confidence_suppressed_action": p.action},
        "rationale_short": f"low confidence: {new_action} instead of {p.action} ({p.rationale_short})",
    })


def generate(state: ClientState, cfg: Config) -> list[Proposal]:
    raw = training_proposals(state, cfg) + nutrition_proposals(state, cfg) + cardio.interference(state)
    return [enforce_low_confidence(score(p, state, cfg)) for p in raw]
