"""Derived ClientState (BUILD_SPEC §6.2). Always rebuilt from events."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from schemas.events import DayType, MacroTargets, PhaseName, TrainingAge

Confidence = Literal["high", "medium", "low"]


class PhaseState(BaseModel):
    phase: PhaseName
    start_date: date
    week: int
    target_rate_pct_bw: float
    planned_weeks: int | None = None
    source: str


class WeeklyWeight(BaseModel):
    window_end: date  # trailing 7-day window ending here (inclusive)
    avg_lb: float | None
    n: int


class WeightTrend(BaseModel):
    weekly: list[WeeklyWeight] = Field(default_factory=list)  # most recent first
    trend_weeks: int = 0  # contiguous recent windows with >= min weigh-ins
    avg_weekly_change_lb: float | None = None
    pct_bw_per_week: float | None = None
    latest_avg_lb: float | None = None


class MaintenanceEstimate(BaseModel):
    kcal: float
    confidence: Confidence
    method: Literal["calibrated"]
    weeks_used: int


class MesoState(BaseModel):
    meso_id: str
    start_date: date
    week: int  # 1-based; weeks_planned + 1 is the deload week
    weeks_planned: int
    rir_target: tuple[int, int] | None  # None during deload
    is_deload_week: bool
    exercises_per_muscle: dict[str, list[str]]
    starting_sets_per_muscle: dict[str, int]
    source: str


class MuscleWeek(BaseModel):
    meso_id: str
    meso_week: int
    week_start: date
    sets: int = 0
    soreness: int | None = None  # max reported for the week
    performance: int | None = None  # derived (§6.3); None in week 1 / no data
    stimulus_total: int | None = None


class Limitation(BaseModel):
    limitation_id: str
    area: str
    description: str
    restrictions: list[str]
    since: date


class TargetChange(BaseModel):
    date: date
    day_type: str
    before: MacroTargets | None
    after: MacroTargets
    note: str = ""
    source: str


class DataQualityFlag(BaseModel):
    code: str
    stream: str
    start: date
    end: date
    detail: str
    severity: Literal["info", "warn", "critical"] = "warn"


class ClientState(BaseModel):
    client_id: str
    as_of: date
    consent: bool = False
    training_age: TrainingAge | None = None
    onboarded: bool = False
    onboarding_date: date | None = None
    phase: PhaseState | None = None
    weight: WeightTrend = Field(default_factory=WeightTrend)
    maintenance: MaintenanceEstimate | None = None
    avg_intake_kcal: float | None = None
    current_macros: dict[DayType, MacroTargets] | None = None
    target_changes: list[TargetChange] = Field(default_factory=list)  # oldest first
    limitations: list[Limitation] = Field(default_factory=list)  # active only
    meso: MesoState | None = None
    last_deload_end: date | None = None
    muscle_weeks: dict[str, list[MuscleWeek]] = Field(default_factory=dict)  # oldest first
    joint_pain: dict[str, int] = Field(default_factory=dict)  # last 7 days, max severity
    latest_checkin: dict | None = None
    checkins_recent: list[dict] = Field(default_factory=list)  # most recent first
    macro_adherence: dict | None = None  # {days_logged, days_hit, pct, window_days, recent: [...]}
    cardio_minutes_by_week: list[float] = Field(default_factory=list)  # most recent first
    cardio_recent: list[dict] = Field(default_factory=list)  # sessions in the lever lookback, oldest first
    cardio_max_sessions_per_week: int | None = None
    cardio_max_minutes_per_session: int | None = None
    data_quality: list[DataQualityFlag] = Field(default_factory=list)
