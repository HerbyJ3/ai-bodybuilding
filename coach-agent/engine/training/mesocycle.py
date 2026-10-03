"""Mesocycle RIR schedule and week arithmetic (training-defaults.json `mesocycle`)."""
from __future__ import annotations

from config.loader import Config
from engine.util import round_half_up


def rir_target(week: int, weeks_planned: int, cfg: Config) -> tuple[int, int] | None:
    """RIR range for an accumulation week; None for the deload week(s) after it.

    Week 1 and the final week come from the JSON; weeks in between are
    interpolated (method in engine-settings `training.rir_interpolation`).
    """
    if week < 1:
        raise ValueError("meso week is 1-based")
    if week > weeks_planned:
        return None
    lo1, hi1 = cfg.training("mesocycle.rir_week1")
    lof, hif = cfg.training("mesocycle.rir_final_week")
    if weeks_planned == 1:
        return (lof, hif)
    method = cfg.setting("training.rir_interpolation")
    if method != "linear":
        raise ValueError(f"unsupported rir_interpolation '{method}'")
    t = (week - 1) / (weeks_planned - 1)
    return (round_half_up(lo1 + (lof - lo1) * t), round_half_up(hi1 + (hif - hi1) * t))


def validate_meso_length(weeks_planned: int, cfg: Config) -> list[str]:
    """Returns warnings (not errors): coach may deliberately plan a short meso."""
    minimum = cfg.training("mesocycle.accumulation_weeks_min")
    if weeks_planned < minimum:
        return [f"weeks_planned {weeks_planned} < accumulation_weeks_min {minimum}"]
    return []
