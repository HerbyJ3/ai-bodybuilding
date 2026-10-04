"""Macros: protein (phase g/lb) -> carbs by day type -> fat remainder, never
below the fat floor (nutrition-defaults.json `protein`, `carbs`, `fat`)."""
from __future__ import annotations

from dataclasses import dataclass, field

from config.loader import Config
from schemas.events import MacroTargets


@dataclass
class MacroResult:
    macros: MacroTargets
    flags: list[str] = field(default_factory=list)


def kcal_of(m: MacroTargets, cfg: Config) -> float:
    k = cfg.setting("energy.kcal_per_g")
    return m.protein_g * k["protein"] + m.carb_g * k["carb"] + m.fat_g * k["fat"]


def protein_g_per_lb(phase: str | None, cfg: Config) -> float:
    if phase in ("cut", "mini_cut"):
        return cfg.nutrition("protein.cut_typical")
    if phase == "gain":
        return cfg.nutrition("protein.gain_typical")
    return cfg.nutrition("protein.default")


def compute(bw_lb: float, calories: float, phase: str | None, day_type: str,
            cfg: Config) -> MacroResult:
    k = cfg.setting("energy.kcal_per_g")
    protein = protein_g_per_lb(phase, cfg) * bw_lb
    carbs = cfg.nutrition(f"carbs.by_day_type.{day_type}.start") * bw_lb
    fat = (calories - protein * k["protein"] - carbs * k["carb"]) / k["fat"]
    fat_floor = cfg.nutrition("fat.minimum") * bw_lb
    carb_floor = cfg.nutrition(f"carbs.by_day_type.{day_type}.minimum") * bw_lb
    flags = []
    if fat < fat_floor:
        # move calories from carbs to fat
        fat = fat_floor
        carbs = (calories - protein * k["protein"] - fat * k["fat"]) / k["carb"]
        if carbs < carb_floor:
            flags.append("carbs_below_day_type_minimum")
            carbs = max(carbs, 0.0)
    elif carbs < carb_floor:  # unreachable with JSON start >= minimum, kept as a guard
        flags.append("carbs_below_day_type_minimum")
    return MacroResult(MacroTargets(protein_g=round(protein), carb_g=round(carbs),
                                    fat_g=round(fat)), flags)


def apply_change(m: MacroTargets, delta_kcal: float, bw_lb: float, day_type: str,
                 cfg: Config) -> MacroResult:
    """Decrease: fat down to its floor, then carbs toward the day-type minimum;
    protein is never reduced (`phases.cut.macro_cut_order`, `protect`).
    Increase: carbs first (`phases.gain.macro_add_order`); adding fat instead
    is a coach call when carb volume hurts adherence."""
    k = cfg.setting("energy.kcal_per_g")
    fat, carbs, flags = m.fat_g, m.carb_g, []
    if delta_kcal >= 0:
        carbs += delta_kcal / k["carb"]
    else:
        need = -delta_kcal
        fat_floor = cfg.nutrition("fat.minimum") * bw_lb
        carb_floor = cfg.nutrition(f"carbs.by_day_type.{day_type}.minimum") * bw_lb
        take_fat = min(need, max(0.0, (fat - fat_floor) * k["fat"]))
        fat -= take_fat / k["fat"]
        need -= take_fat
        take_carb = min(need, max(0.0, (carbs - carb_floor) * k["carb"]))
        carbs -= take_carb / k["carb"]
        need -= take_carb
        if need > 0.5:
            flags.append("floors_reached_change_not_fully_applied")
    return MacroResult(MacroTargets(protein_g=m.protein_g, carb_g=round(carbs),
                                    fat_g=round(fat)), flags)
