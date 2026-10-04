"""Maintenance calibration from history (BUILD_SPEC §7.3).

true_maintenance ≈ avg_daily_intake − (avg_weekly_change_lb × kcal_per_lb / 7)

The pre-history formula estimate (BMR × activity) is an open [DECIDE] item,
so `initial_estimate` refuses rather than invent one.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from config.loader import Config
from engine.util import window_index
from schemas.state import Confidence, MaintenanceEstimate, WeightTrend


class MaintenanceFormulaUndecided(RuntimeError):
    pass


def initial_estimate(*_: object, **__: object) -> float:
    raise MaintenanceFormulaUndecided(
        "Initial maintenance formula is [DECIDE] (BUILD_SPEC §7.3, OPEN_ITEMS)")


def _confidence(intake_days: list[int], weigh_ins: list[int], cfg: Config) -> Confidence:
    for level in ("high", "medium"):
        t = cfg.setting(f"maintenance_confidence.{level}")
        if (min(intake_days) >= t["min_intake_days_per_week"]
                and min(weigh_ins) >= t["min_weigh_ins_per_week"]):
            return level  # type: ignore[return-value]
    return "low"


def calibrate(trend: WeightTrend, intake: list[tuple[date, float]], as_of: date,
              cfg: Config) -> MaintenanceEstimate | None:
    """`intake` is (date, calories) per logged day."""
    weeks = cfg.nutrition("tracking.assess_window_weeks")[1]
    if trend.trend_weeks < weeks or trend.avg_weekly_change_lb is None:
        return None
    per_window: dict[int, dict[date, float]] = defaultdict(dict)
    for d, kcal in intake:
        k = window_index(as_of, d)
        if 0 <= k < weeks:
            per_window[k][d] = kcal
    if any(not per_window[k] for k in range(weeks)):
        return None
    all_days = [v for k in range(weeks) for v in per_window[k].values()]
    avg_intake = sum(all_days) / len(all_days)
    kcal_per_lb = cfg.nutrition("tracking.kcal_per_lb_tissue")
    kcal = avg_intake - trend.avg_weekly_change_lb * kcal_per_lb / 7
    conf = _confidence([len(per_window[k]) for k in range(weeks)],
                       [trend.weekly[k].n for k in range(weeks)], cfg)
    return MaintenanceEstimate(kcal=round(kcal), confidence=conf, method="calibrated",
                               weeks_used=weeks)
