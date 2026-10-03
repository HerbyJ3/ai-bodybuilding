"""MEV estimator (training-defaults.json `mev_estimator`, SPHT table 2.2)."""
from __future__ import annotations

from config.loader import Config
from engine.training.outcome import Outcome, parse
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState

RULE_ID = "training.mev_estimator"


def band_outcome(total: int, cfg: Config) -> tuple[Outcome, dict]:
    for band in cfg.training("mev_estimator.score_bands"):
        lo, hi = band["total"]
        if lo <= total <= hi:
            return parse(band["action"]), band
    raise ValueError(f"stimulus total {total} outside 0-9")


def evaluate(state: ClientState, cfg: Config) -> list[Proposal]:
    """Uses week-1 stimulus of the current meso; decides week 2 volume."""
    m = state.meso
    if m is None or m.week > 2:
        return []
    out = []
    for muscle, weeks in sorted(state.muscle_weeks.items()):
        w1 = next((w for w in weeks if w.meso_id == m.meso_id and w.meso_week == 1), None)
        if w1 is None or w1.stimulus_total is None:
            continue
        outcome, band = band_outcome(w1.stimulus_total, cfg)
        if outcome.kind == "defer":
            continue
        out.append(new_proposal(
            state.client_id, state.as_of, RULE_ID, f"volume.{muscle}",
            outcome.kind, rationale=f"{muscle}: week-1 stimulus {w1.stimulus_total}/9, "
                                    f"{band['meaning']}",
            current_value=w1.sets, proposed_value=outcome.proposed_value(),
            inputs_used={"muscle": muscle, "stimulus_total": w1.stimulus_total,
                         "meso_id": m.meso_id},
            config_keys=["mev_estimator.score_bands"]))
    return out
