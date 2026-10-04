"""Event models (BUILD_SPEC §6.1). Events are append-only and immutable."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Rating03 = Annotated[int, Field(ge=0, le=3)]
Rating15 = Annotated[int, Field(ge=1, le=5)]
PhaseName = Literal["gain", "cut", "maintenance", "mini_cut", "recomp"]
DayType = Literal["non_training", "light", "moderate", "hard"]
TrainingAge = Literal["beginner", "intermediate", "advanced"]

EVENT_NAMESPACE = uuid.UUID("6f1c2a52-6a0e-4c38-9d55-2f0b8e1c7a10")


class Source(str, Enum):
    client = "client"
    coach = "coach"
    import_ = "import"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SetLogged(_Payload):
    exercise_id: str
    muscle: str
    load: float = Field(ge=0)
    reps: int = Field(ge=0)
    rir: float  # out-of-range values are flagged by data_quality, not rejected
    set_index: int = Field(ge=0)
    session_id: str


class SessionCompleted(_Payload):
    session_id: str
    date: date
    muscles_trained: list[str]


class StimulusRated(_Payload):
    session_id: str
    muscle: str
    mind_muscle: Rating03
    pump: Rating03
    disruption: Rating03


class SorenessRated(_Payload):
    muscle: str
    soreness: Rating03
    refers_to_session_id: str | None = None  # imports may not carry it


class JointPainReported(_Payload):
    joint: str
    severity: Rating03
    exercise_id: str | None = None


class WeighIn(_Payload):
    weight: float = Field(gt=0)
    unit: Literal["lb", "kg"]
    conditions: str = "unspecified"


class IntakeLogged(_Payload):
    date: date
    calories: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    carb_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)


class WeeklyCheckin(_Payload):
    adherence_pct: float | None = Field(default=None, ge=0, le=100)  # optional; daily macro logs can stand in
    hunger: Rating15
    energy: Rating15
    sleep: Rating15 | None = None  # 1-5 quality rating (CSV imports)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)  # dashboard dropdown
    training_feel: Literal["crap", "good", "fantastic"] | None = None
    notes: str = ""


MacroKey = Literal["protein_g", "carb_g", "fat_g"]


class MacroAdherenceLogged(_Payload):
    """Coach/client answer to 'Macros hit?' for one day; when missed, grams over (+) or under (-)."""
    date: date
    hit: bool
    off_by: dict[MacroKey, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "MacroAdherenceLogged":
        if self.hit and self.off_by:
            raise ValueError("a day marked as hit has no misses")
        if not self.hit and not any(self.off_by.values()):
            raise ValueError("a missed day needs at least one macro and amount")
        return self


class CardioLogged(_Payload):
    date: date
    modality: str
    minutes: float = Field(ge=0)
    intensity: Literal["low", "mod", "high"]
    est_kcal: float | None = Field(default=None, ge=0)


class PhaseStarted(_Payload):
    phase: PhaseName
    target_rate_pct_bw: float = Field(ge=0)
    planned_weeks: int = Field(gt=0)


class MesoStarted(_Payload):
    meso_id: str
    weeks_planned: int = Field(gt=0)  # accumulation weeks, deload not included
    exercises_per_muscle: dict[str, list[str]]
    starting_sets_per_muscle: dict[str, int]


class ProposalDecided(_Payload):
    proposal_id: str
    decision: Literal["approved", "rejected", "modified"]
    coach_note: str = ""
    proposal: dict[str, Any] | None = None  # snapshot of what was decided (audit + LLM layer)
    final_value: Any = None  # the value that takes effect (coach's value when modified)


# --- added for mid-program onboarding (BUILD_SPEC §13) -----------------------

class ConsentRecorded(_Payload):
    granted: bool
    scope: list[str]
    note: str = ""


class DeloadCompleted(_Payload):
    start_date: date
    end_date: date
    meso_id: str | None = None

    @model_validator(mode="after")
    def _order(self) -> "DeloadCompleted":
        if self.end_date < self.start_date:
            raise ValueError("deload end_date before start_date")
        return self


class MacroTargets(_Payload):
    protein_g: float = Field(ge=0)
    carb_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)


class NutritionTargetsSet(_Payload):
    """Prescribed macros. Day types not listed keep their previous targets."""
    macros_by_day_type: dict[DayType, MacroTargets]
    proposal_id: str | None = None
    note: str = ""


class LimitationRecorded(_Payload):
    """A physical limitation that restricts movement or equipment (injury, condition).
    Re-recording the same limitation_id replaces it; active=False resolves it."""
    limitation_id: str = Field(min_length=1)
    area: str = Field(min_length=1)  # e.g. "cervical spine (C5/C6)"
    description: str = ""
    restrictions: list[str] = Field(min_length=1)  # e.g. ["no free weights"]
    active: bool = True


class ProfileUpdated(_Payload):
    training_age: TrainingAge | None = None
    # per-client cardio ceilings (schedule, limitations); None = engine default
    cardio_max_sessions_per_week: int | None = Field(default=None, ge=0)
    cardio_max_minutes_per_session: int | None = Field(default=None, ge=0)


class OnboardingCompleted(_Payload):
    as_of: date
    meso_id: str | None  # None: client has no mesocycle structure yet
    current_meso_week: int | None = Field(default=None, ge=1)
    last_deload_date: date | None
    phase: PhaseName
    current_phase_week: int = Field(ge=1)
    imported_event_counts: dict[str, int]
    data_quality_codes: list[str]


PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "set_logged": SetLogged,
    "session_completed": SessionCompleted,
    "stimulus_rated": StimulusRated,
    "soreness_rated": SorenessRated,
    "joint_pain_reported": JointPainReported,
    "weigh_in": WeighIn,
    "intake_logged": IntakeLogged,
    "weekly_checkin": WeeklyCheckin,
    "cardio_logged": CardioLogged,
    "phase_started": PhaseStarted,
    "meso_started": MesoStarted,
    "proposal_decided": ProposalDecided,
    "consent_recorded": ConsentRecorded,
    "deload_completed": DeloadCompleted,
    "nutrition_targets_set": NutritionTargetsSet,
    "profile_updated": ProfileUpdated,
    "limitation_recorded": LimitationRecorded,
    "macro_adherence_logged": MacroAdherenceLogged,
    "onboarding_completed": OnboardingCompleted,
}
EventType = Literal[
    "set_logged", "session_completed", "stimulus_rated", "soreness_rated",
    "joint_pain_reported", "weigh_in", "intake_logged", "weekly_checkin",
    "cardio_logged", "phase_started", "meso_started", "proposal_decided",
    "consent_recorded", "deload_completed", "nutrition_targets_set",
    "profile_updated", "onboarding_completed", "limitation_recorded", "macro_adherence_logged",
]


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc)


class Event(BaseModel):
    """`timestamp` is when the event took effect (backdated for imports);
    `recorded_at` is when it was written to the store."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    client_id: str = Field(min_length=1)
    type: EventType
    timestamp: datetime
    source: Source
    recorded_at: datetime
    payload: Any

    @field_validator("timestamp", "recorded_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _utc(v)

    @model_validator(mode="after")
    def _typed_payload(self) -> "Event":
        model = PAYLOAD_MODELS[self.type]
        if not isinstance(self.payload, model):
            object.__setattr__(self, "payload", model.model_validate(self.payload))
        return self

    @property
    def day(self) -> date:
        return self.timestamp.date()

    def payload_json(self) -> str:
        return json.dumps(self.payload.model_dump(mode="json"), sort_keys=True)


def make_event(
    client_id: str,
    type: str,
    timestamp: datetime | date,
    payload: dict[str, Any] | _Payload,
    source: Source | str = Source.client,
    recorded_at: datetime | None = None,
) -> Event:
    """Builds an event with a content-derived id, so re-importing the same
    row is idempotent (the store ignores duplicate ids)."""
    if not isinstance(timestamp, datetime):
        timestamp = datetime.combine(timestamp, time(12, 0), tzinfo=timezone.utc)
    model = PAYLOAD_MODELS[type]
    p = payload if isinstance(payload, model) else model.model_validate(payload)
    canonical = json.dumps(p.model_dump(mode="json"), sort_keys=True)
    ts = _utc(timestamp)
    event_id = str(uuid.uuid5(EVENT_NAMESPACE, f"{client_id}|{type}|{ts.isoformat()}|{canonical}"))
    return Event(
        event_id=event_id,
        client_id=client_id,
        type=type,
        timestamp=ts,
        source=Source(source),
        recorded_at=recorded_at or datetime.now(timezone.utc),
        payload=p,
    )
