"""Set progression (training-defaults.json `set_progression`, SPHT table 2.3)
with the cut coupling from `diet_phase_coupling`."""
from __future__ import annotations

from config.loader import Config
from engine.training.outcome import Outcome, most_conservative, parse
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState, MuscleWeek

RULE_ID = "training.set_progression"


def lookup(soreness: int, performance: int, cfg: Config) -> Outcome:
    if not (0 <= soreness <= 3 and 0 <= performance <= 3):
        raise ValueError("soreness and performance are 0-3")
    outcome = parse(cfg.training("set_progression.matrix")[str(soreness)][performance])
    # rule_override: performance >= 2 never adds sets
    if performance >= 2 and outcome.kind == "add_sets":
        outcome = Outcome("hold")
    return outcome


def decide(soreness: int, performance: int | None, phase: str | None,
           training_age: str | None, cfg: Config) -> Outcome:
    """`performance=None` means meso week 1: scored as 1-2 per the JSON note;
    the more conservative of the two cells is used."""
    if performance is None:
        outcome = most_conservative(lookup(soreness, 1, cfg), lookup(soreness, 2, cfg))
    else:
        outcome = lookup(soreness, performance, cfg)
    if outcome.kind == "add_sets" and phase in ("cut", "mini_cut") and training_age != "beginner":
        cap = cfg.setting("training.cut_max_set_addition_non_beginner")
        outcome = Outcome("add_sets", min(outcome.add_min, cap), min(outcome.add_max, cap))
    return outcome


def latest_rated_week(state: ClientState, muscle: str) -> MuscleWeek | None:
    m = state.meso
    if m is None:
        return None
    weeks = [w for w in state.muscle_weeks.get(muscle, [])
             if w.meso_id == m.meso_id and w.soreness is not None
             and (w.performance is not None or w.meso_week == 1)
             and w.meso_week >= m.week - 1]
    return weeks[-1] if weeks else None


def evaluate(state: ClientState, cfg: Config, skip_muscles: set[str] = frozenset()) -> list[Proposal]:
    m = state.meso
    if m is None or m.is_deload_week:
        return []
    phase = state.phase.phase if state.phase else None
    out = []
    for muscle in sorted(state.muscle_weeks):
        if muscle in skip_muscles or (w := latest_rated_week(state, muscle)) is None:
            continue
        outcome = decide(w.soreness, w.performance, phase, state.training_age, cfg)  # type: ignore[arg-type]
        perf = "1-2 (week 1)" if w.performance is None else str(w.performance)
        out.append(new_proposal(
            state.client_id, state.as_of, RULE_ID, f"volume.{muscle}", outcome.kind,
            rationale=f"{muscle}: soreness {w.soreness}, performance {perf} -> {outcome.kind}",
            current_value=w.sets, proposed_value=outcome.proposed_value(),
            inputs_used={"muscle": muscle, "soreness": w.soreness, "performance": w.performance,
                         "meso_id": w.meso_id, "meso_week": w.meso_week, "phase": phase,
                         "training_age": state.training_age},
            config_keys=["set_progression.matrix", "set_progression.rule_override"]
            + (["diet_phase_coupling.cut", "engine-settings:training"] if phase in ("cut", "mini_cut") else [])))
    return out
