"""Cold-start gates for mid-program onboarding (BUILD_SPEC §13.5).

Nutrition: proposals run once >= tracking.min_weeks_before_trend weeks of
weigh-ins exist (same requirement as §7.2 step 2, applied to every client).

Training: for onboarded clients, autoregulation proposals (MEV estimator,
set progression, performance-triggered deload) stay `hold` per muscle until
it has `cold_start.training_min_rated_weeks` rated weeks (soreness AND derived
performance) in a run with no more than `max_days_between_rated_weeks`
between them. Imported history counts if it passes that run test. Once a
muscle qualifies it stays qualified.
"""
from __future__ import annotations

from dataclasses import dataclass

from config.loader import Config
from schemas.state import ClientState


@dataclass(frozen=True)
class GateStatus:
    ready: bool
    have_weeks: int
    need_weeks: int


def nutrition_status(state: ClientState, cfg: Config) -> GateStatus:
    need = cfg.nutrition("tracking.min_weeks_before_trend")
    return GateStatus(state.weight.trend_weeks >= need, state.weight.trend_weeks, need)


def _longest_rated_run(state: ClientState, muscle: str, max_days: int) -> int:
    rated = [w.week_start for w in state.muscle_weeks.get(muscle, [])
             if w.soreness is not None and w.performance is not None]
    best = run = 0
    prev = None
    for d in rated:
        run = run + 1 if prev is not None and (d - prev).days <= max_days else 1
        best, prev = max(best, run), d
    return best


def training_applies(state: ClientState, cfg: Config) -> bool:
    scope = cfg.setting("cold_start.applies_to")
    if scope == "all_clients":
        return True
    if scope == "onboarded_clients":
        return state.onboarded
    raise ValueError(f"unknown cold_start.applies_to '{scope}'")


def training_status(state: ClientState, cfg: Config) -> dict[str, GateStatus]:
    need = cfg.setting("cold_start.training_min_rated_weeks")
    max_days = cfg.setting("cold_start.max_days_between_rated_weeks")
    applies = training_applies(state, cfg)
    out = {}
    for muscle in sorted(state.muscle_weeks):
        have = _longest_rated_run(state, muscle, max_days)
        out[muscle] = GateStatus(not applies or have >= need, have, need)
    return out
