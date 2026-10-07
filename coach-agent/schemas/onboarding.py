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
    planned_weeks: int | None = Field(default=None, gt=0)  # optional (owner 2026-10-07)
    current_phase_week: int = Field(ge=1)


class MesoInput(_In):
    meso_id: str
    weeks_planned: int = Field(gt=0)
    current_week: int = Field(ge=1)
    last_deload_date: date | None  # date the last deload week ended; null = none known
    exercises_per_muscle: dict[str, list[str]] | None = None  # inferred from history if omitted
    starting_sets_per_muscle: dict[str, int] | None = None


class PastMesoInput(_In):
    """A completed mesocycle in the imported history (enables cross-meso comparison)."""
    meso_id: str
    start_date: date
    weeks_planned: int = Field(gt=0)


class PastPhaseInput(_In):
    phase: PhaseName
    start_date: date
    target_rate_pct_bw: float = Field(ge=0)
    planned_weeks: int | None = Field(default=None, gt=0)  # optional (owner 2026-10-07)


class LimitationInput(_In):
    limitation_id: str
    area: str
    description: str = ""
    restrictions: list[str] = Field(min_length=1)
    since: date | None = None  # defaults to as_of


class ImportInput(_In):
    csv_dir: str
    mapping: str


class OnboardingConfig(_In):
    client_id: str = Field(min_length=1)
    as_of: date
    consent: ConsentInput
    training_age: TrainingAge | None = None
    cardio_max_sessions_per_week: int | None = Field(default=None, ge=0)  # schedule/limitation ceiling
    cardio_max_minutes_per_session: int | None = Field(default=None, ge=0)
    phase: PhaseInput
    meso: MesoInput | None = None  # None: no mesocycle structure (training rules stay inactive)
    limitations: list[LimitationInput] = Field(default_factory=list)
    nutrition_targets: dict[DayType, MacroTargets] | None = None
    past_mesos: list[PastMesoInput] = Field(default_factory=list)
    past_phases: list[PastPhaseInput] = Field(default_factory=list)
    history: ImportInput
