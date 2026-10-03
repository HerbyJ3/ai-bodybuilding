"""Input for mid-program onboarding (BUILD_SPEC §13)."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from schemas.events import DayType, MacroTargets, PhaseName, TrainingAge


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentInput(_In):
    granted: bool
    scope: list[str]
    note: str = ""


class PhaseInput(_In):
    phase: PhaseName
    target_rate_pct_bw: float = Field(ge=0)
    planned_weeks: int = Field(gt=0)
    current_phase_week: int = Field(ge=1)


class MesoInput(_In):
    meso_id: str
    weeks_planned: int = Field(gt=0)
    current_week: int = Field(ge=1)
    last_deload_date: date | None  # date the last deload week ended; null = none known
    exercises_per_muscle: dict[str, list[str]] | None = None  # inferred from history if omitted
    starting_sets_per_muscle: dict[str, int] | None = None


class ImportInput(_In):
    csv_dir: str
    mapping: str


class OnboardingConfig(_In):
    client_id: str = Field(min_length=1)
    as_of: date
    consent: ConsentInput
    training_age: TrainingAge | None = None
    phase: PhaseInput
    meso: MesoInput
    nutrition_targets: dict[DayType, MacroTargets] | None = None
    history: ImportInput
