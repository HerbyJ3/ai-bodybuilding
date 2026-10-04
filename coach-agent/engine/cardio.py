"""PROVISIONAL cardio rules (BUILD_SPEC §7.4; not from either source book).

- Cardio counts toward day-type classification.
- Leg soreness/performance worsening while cardio volume rises -> flag only.
- Never proposes adding cardio.
"""
from __future__ import annotations

from config.loader import Config
from schemas.proposals import Proposal, new_proposal
from schemas.state import ClientState

RULE_ID = "cardio.interference"
ORDER = ["non_training", "light", "moderate", "hard"]
LEG_MUSCLES = {"quads", "hamstrings", "glutes", "calves"}


def cardio_kcal(minutes: float, intensity: str, est_kcal: float | None, cfg: Config) -> float:
    if est_kcal is not None:
        return est_kcal
    return minutes * cfg.setting(f"cardio.kcal_per_min_by_intensity.{intensity}")


def classify_day(working_sets: int, cardio_kcal_total: float, phase: str | None,
                 cfg: Config) -> str:
    dc = cfg.nutrition("day_classification")
    light_max = dc["light"]["working_sets_max"]
    mod_lo, mod_hi = dc["moderate"]["working_sets"]
    hard_min = dc["hard"]["working_sets_min"]
    lean_light = phase in ("cut", "mini_cut")  # 'when unsure' rule
    if working_sets <= 0:
        sets_class = "non_training"
    elif working_sets < mod_lo:
        sets_class = "light"
    elif working_sets == light_max == mod_lo:
        sets_class = "light" if lean_light else "moderate"
    elif working_sets < hard_min:
        sets_class = "moderate"
    elif working_sets == mod_hi == hard_min:
        sets_class = "moderate" if lean_light else "hard"
    else:
        sets_class = "hard"
    if cardio_kcal_total <= 0:
        return sets_class
    base = {"non_training": 0, "light": dc["light"]["approx_kcal_burn"],
            "moderate": dc["moderate"]["approx_kcal_burn"],
            "hard": dc["moderate"]["approx_kcal_burn"]}[sets_class]
    burn = base + cardio_kcal_total
    burn_class = ("moderate" if burn >= dc["moderate"]["approx_kcal_burn"]
                  else "light" if burn >= dc["light"]["approx_kcal_burn"] else "non_training")
    return max(sets_class, burn_class, key=ORDER.index)


def interference(state: ClientState) -> list[Proposal]:
    c = state.cardio_minutes_by_week
    if len(c) < 2 or c[0] <= c[1]:
        return []
    worse = []
    for muscle in sorted(LEG_MUSCLES & state.muscle_weeks.keys()):
        ws = state.muscle_weeks[muscle]
        if len(ws) < 2:
            continue
        a, b = ws[-2], ws[-1]
        if ((a.soreness is not None and b.soreness is not None and b.soreness > a.soreness)
                or (a.performance is not None and b.performance is not None
                    and b.performance > a.performance)):
            worse.append(muscle)
    if not worse:
        return []
    return [new_proposal(state.client_id, state.as_of, RULE_ID, "cardio", "flag",
                         rationale=f"cardio up {c[1]:.0f}->{c[0]:.0f} min/wk while "
                                   f"{', '.join(worse)} worsened; possible interference",
                         inputs_used={"cardio_minutes_last_2_weeks": [c[1], c[0]],
                                      "muscles": worse},
                         config_keys=["engine-settings:cardio"])]
