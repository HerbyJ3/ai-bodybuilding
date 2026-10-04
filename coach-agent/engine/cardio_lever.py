"""Cardio as a calorie lever in a cut (owner decision 2026-10-04; amends BUILD_SPEC §7.4).

When a cut is losing too slowly, the cheapest reversible lever goes first:
  1. frequency: one more session per week, at the client's usual session length
  2. duration: longer sessions, up to the per-client ceiling
then calories. A lever only counts if its weekly effect clears the scale's noise
floor; otherwise the next lever is tried. The client's current modality is kept,
so recorded limitations (e.g. "incline treadmill walking only") stay respected.
Always a proposal for coach approval; all numbers are provisional settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.loader import Config
from schemas.state import ClientState


@dataclass(frozen=True)
class CardioStep:
    lever: str  # "frequency" | "duration"
    current: dict[str, Any]
    proposed: dict[str, Any]
    kcal_per_day: float
    lb_per_week: float


def current_cardio(state: ClientState, cfg: Config) -> dict[str, Any] | None:
    sessions = state.cardio_recent
    if not sessions:
        return None
    weeks = cfg.setting("cardio_lever.lookback_days") / 7
    minutes = sum(s["minutes"] for s in sessions) / len(sessions)
    per_min = []
    for s in sessions:
        if s["minutes"] <= 0:
            continue
        kcal = s["est_kcal"] if s["est_kcal"] is not None else \
            s["minutes"] * cfg.setting(f"cardio.kcal_per_min_by_intensity.{s['intensity']}")
        per_min.append(kcal / s["minutes"])
    if not per_min:
        return None
    latest = sessions[-1]
    return {"sessions_per_week": round(len(sessions) / weeks, 1), "minutes_per_session": round(minutes),
            "kcal_per_minute": round(sum(per_min) / len(per_min), 2),
            "modality": latest["modality"], "intensity": latest["intensity"]}


def next_step(state: ClientState, cfg: Config) -> tuple[CardioStep | None, list[dict[str, Any]]]:
    """Returns the first lever that clears the noise floor, plus the levers considered."""
    s = cfg.setting("cardio_lever")
    considered: list[dict[str, Any]] = []
    if not s["enabled"]:
        return None, [{"lever": "cardio", "skipped": "disabled"}]
    cur = current_cardio(state, cfg)
    if cur is None:
        return None, [{"lever": "cardio", "skipped": "no recent cardio sessions to extend"}]
    kcal_lb = cfg.nutrition("tracking.kcal_per_lb_tissue")
    max_sessions = state.cardio_max_sessions_per_week if state.cardio_max_sessions_per_week is not None \
        else s["default_max_sessions_per_week"]
    max_minutes = state.cardio_max_minutes_per_session if state.cardio_max_minutes_per_session is not None \
        else s["default_max_minutes_per_session"]
    sessions = round(cur["sessions_per_week"])
    candidates = []
    if sessions < max_sessions:
        weekly = cur["minutes_per_session"] * cur["kcal_per_minute"]
        candidates.append(("frequency", {**cur, "sessions_per_week": sessions + 1}, weekly))
    else:
        considered.append({"lever": "frequency", "skipped": f"at ceiling ({max_sessions}/week)"})
    room = max_minutes - cur["minutes_per_session"]
    if room > 0:
        add = min(s["duration_step_minutes"], room)
        weekly = max(sessions, 1) * add * cur["kcal_per_minute"]
        candidates.append(("duration", {**cur, "minutes_per_session": cur["minutes_per_session"] + add}, weekly))
    else:
        considered.append({"lever": "duration", "skipped": f"at ceiling ({max_minutes} min)"})
    for lever, proposed, weekly in candidates:
        lb = weekly / kcal_lb
        if lb < s["noise_floor_lb_per_week"]:
            considered.append({"lever": lever, "skipped": f"~{lb:.2f} lb/week is below the "
                                                          f"{s['noise_floor_lb_per_week']} lb/week noise floor"})
            continue
        return CardioStep(lever, cur, proposed, round(weekly / 7), round(lb, 2)), considered
    return None, considered
