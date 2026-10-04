"""Fatigue management: deload triggers and structure
(training-defaults.json `fatigue_management`, `mesocycle`)."""
from __future__ import annotations

from config.loader import Config
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState

RULE_ID = "training.fatigue.deload"


def deload_structure(cfg: Config) -> dict:
    d = cfg.training("fatigue_management.deload")
    return {"length": d["length"], "first_half": d["first_half"],
            "second_half": d["second_half"], "exercise_options": d["exercise_options"]}


def schedule_trigger(state: ClientState) -> str | None:
    """Schedule-based trigger: RIR reaches the final 0-1 week, or the meso ran past plan."""
    m = state.meso
    if m is None:
        return None
    deloaded = state.last_deload_end is not None and state.last_deload_end >= m.start_date
    if m.week == m.weeks_planned:
        return "final accumulation week (RIR at final-week target); deload next week"
    if m.week > m.weeks_planned and not deloaded:
        return f"meso week {m.week} is past the {m.weeks_planned} planned accumulation weeks"
    return None


def evaluate(state: ClientState, cfg: Config, recovery_muscles: list[str],
             include_performance_trigger: bool = True) -> list[Proposal]:
    m = state.meso
    if m is None:
        return []
    reasons: list[str] = []
    if (r := schedule_trigger(state)):
        reasons.append(r)
    needed = cfg.setting("training.deload_min_muscles_needing_recovery")
    if include_performance_trigger and len(recovery_muscles) >= needed:
        reasons.append(f"{len(recovery_muscles)} muscles need recovery "
                       f"({', '.join(sorted(recovery_muscles))}): MRV likely hit")
    if not reasons:
        return []
    return [new_proposal(
        state.client_id, state.as_of, RULE_ID, "training.deload", "deload",
        rationale="; ".join(reasons), current_value={"meso_id": m.meso_id, "week": m.week},
        proposed_value=deload_structure(cfg),
        inputs_used={"meso_week": m.week, "weeks_planned": m.weeks_planned,
                     "rir_target": m.rir_target, "recovery_muscles": sorted(recovery_muscles)},
        config_keys=["fatigue_management.deload", "mesocycle.rir_final_week",
                     "engine-settings:training.deload_min_muscles_needing_recovery"])]
