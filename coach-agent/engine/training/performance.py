"""Derived 0–3 performance score (BUILD_SPEC §6.3).

Per set, compared with the same exercise/set_index last meso week:
  load dropped or fewer reps            -> 3 (couldn't match last week)
  reps matched but RIR below target     -> 2 (needed to go past target RIR)
  beat last week's reps by >= 2         -> 0
  otherwise (beat by 0-1)               -> 1
Scores are averaged across all sets of the exercise, then across exercises.
Week 1 has no prior week: returns None (JSON: 'score week 1 performance as 1-2').
"""
from __future__ import annotations

from collections.abc import Sequence

from schemas.events import SetLogged
from engine.util import round_half_up


def set_score(cur: SetLogged, prev: SetLogged, rir_target: tuple[int, int]) -> int:
    if cur.load < prev.load or cur.reps < prev.reps:
        return 3
    if cur.rir < rir_target[0]:
        return 2
    if cur.reps - prev.reps >= 2:
        return 0
    return 1


def exercise_score(cur: Sequence[SetLogged], prev: Sequence[SetLogged],
                   rir_target: tuple[int, int]) -> float | None:
    prev_by_idx = {s.set_index: s for s in prev}
    scores = [set_score(s, prev_by_idx[s.set_index], rir_target)
              for s in cur if s.set_index in prev_by_idx]
    return sum(scores) / len(scores) if scores else None


def muscle_score(cur_by_ex: dict[str, list[SetLogged]], prev_by_ex: dict[str, list[SetLogged]],
                 rir_target: tuple[int, int] | None) -> int | None:
    if rir_target is None:
        return None
    ex_scores = [sc for ex, sets in cur_by_ex.items()
                 if (sc := exercise_score(sets, prev_by_ex.get(ex, []), rir_target)) is not None]
    if not ex_scores:
        return None
    return min(3, round_half_up(sum(ex_scores) / len(ex_scores)))
