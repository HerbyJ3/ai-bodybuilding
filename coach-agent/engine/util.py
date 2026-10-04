from __future__ import annotations

import math
from datetime import date, timedelta

LB_PER_KG = 2.20462


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def to_lb(weight: float, unit: str) -> float:
    return weight * LB_PER_KG if unit == "kg" else weight


def week_index(start: date, d: date) -> int:
    """1-based week number of `d` in a block starting on `start`."""
    return (d - start).days // 7 + 1


def window_index(as_of: date, d: date) -> int:
    """0 = trailing 7 days ending at as_of (inclusive), 1 = the 7 before, ..."""
    return (as_of - d).days // 7


def window_bounds(as_of: date, k: int) -> tuple[date, date]:
    end = as_of - timedelta(days=7 * k)
    return end - timedelta(days=6), end
