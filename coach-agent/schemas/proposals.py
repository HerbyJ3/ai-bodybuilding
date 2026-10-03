"""Proposal object (BUILD_SPEC §6.4)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from schemas.state import Confidence

PROPOSAL_NAMESPACE = uuid.UUID("0b7d0d4e-6b5c-4b8e-9c3f-6a2a1e9b4d21")

Action = Literal[
    "add_sets", "hold", "recovery", "reduce_sets", "deload",
    "decrease_calories", "increase_calories", "adherence_intervention",
    "transition_phase", "set_macros", "flag",
]
# Actions a low-confidence proposal may never carry (§7.5).
ESCALATING_ACTIONS = {"add_sets", "decrease_calories"}


class Proposal(BaseModel):
    proposal_id: str
    client_id: str
    rule_id: str
    target: str
    action: Action
    current_value: Any = None
    proposed_value: Any = None
    inputs_used: dict[str, Any] = Field(default_factory=dict)
    config_keys: list[str] = Field(default_factory=list)
    confidence: Confidence = "high"
    data_quality_flags: list[str] = Field(default_factory=list)
    rationale_short: str
    status: Literal["pending", "approved", "rejected", "modified"] = "pending"
    created_at: datetime


def new_proposal(client_id: str, as_of: date, rule_id: str, target: str, action: Action,
                 rationale: str, **kw: Any) -> Proposal:
    pid = str(uuid.uuid5(PROPOSAL_NAMESPACE, f"{client_id}|{rule_id}|{target}|{as_of.isoformat()}"))
    created = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)
    return Proposal(proposal_id=pid, client_id=client_id, rule_id=rule_id, target=target,
                    action=action, rationale_short=rationale, created_at=created, **kw)
