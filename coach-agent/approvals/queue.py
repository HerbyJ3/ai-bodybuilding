"""Approval queue (BUILD_SPEC §9, M7).

- `refresh` runs the rules engine and stores proposal snapshots in a
  `proposals` table (a cache keyed by the deterministic proposal_id).
- Status is never stored there: it is derived from `proposal_decided` events,
  so the queue can always be rebuilt from the event log.
- A decision writes `proposal_decided` (source=coach) with a snapshot of the
  proposal, plus the events that make an approved change take effect.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from config.loader import Config
from engine import proposals as engine
from engine.state_builder import build_state
from schemas.events import Event, MacroTargets, Source, make_event
from schemas.proposals import Proposal
from store.event_store import EventStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    target      TEXT NOT NULL,
    body        TEXT NOT NULL
);
"""
# Holds are informational: nothing to approve. Everything else needs a decision.
ACTIONABLE = {"add_sets", "recovery", "reduce_sets", "deload", "decrease_calories",
              "increase_calories", "adherence_intervention", "transition_phase", "flag"}


class QueueError(ValueError):
    pass


@dataclass
class QueueItem:
    proposal: Proposal
    status: str  # pending | approved | rejected | modified | superseded | info


class ApprovalQueue:
    def __init__(self, store: EventStore, cfg: Config) -> None:
        self.store, self.cfg = store, cfg
        store._db.executescript(_SCHEMA)

    # --- generation -------------------------------------------------------------
    def refresh(self, client_id: str, as_of: date) -> list[Proposal]:
        if not self.store.has_consent(client_id):
            raise QueueError(f"no consent recorded for {client_id}")
        st = build_state(self.store.read(client_id), as_of, self.cfg)
        props = engine.generate(st, self.cfg)
        with self.store._db:
            for p in props:
                self.store._db.execute(
                    "INSERT OR IGNORE INTO proposals VALUES (?,?,?,?,?)",
                    (p.proposal_id, p.client_id, p.created_at.isoformat(), p.target,
                     p.model_dump_json()))
        return props

    # --- reading -------------------------------------------------------------------
    def _decisions(self, client_id: str) -> dict[str, Event]:
        return {e.payload.proposal_id: e for e in self.store.read(client_id)
                if e.type == "proposal_decided"}

    def items(self, client_id: str) -> list[QueueItem]:
        rows = self.store._db.execute(
            "SELECT body FROM proposals WHERE client_id = ? ORDER BY created_at, target",
            (client_id,)).fetchall()
        props = [Proposal.model_validate_json(r[0]) for r in rows]
        decided = self._decisions(client_id)
        latest: dict[str, datetime] = {}
        for p in props:
            latest[p.target] = max(latest.get(p.target, p.created_at), p.created_at)
        out = []
        for p in props:
            if p.proposal_id in decided:
                status = decided[p.proposal_id].payload.decision
            elif p.action not in ACTIONABLE:
                status = "info"
            elif p.created_at < latest[p.target]:
                status = "superseded"
            else:
                status = "pending"
            out.append(QueueItem(p, status))
        return out

    def pending(self, client_id: str) -> list[QueueItem]:
        return [i for i in self.items(client_id) if i.status == "pending"]

    def get(self, proposal_id: str) -> QueueItem:
        row = self.store._db.execute("SELECT client_id FROM proposals WHERE proposal_id = ?",
                                     (proposal_id,)).fetchone()
        if not row:
            raise QueueError(f"unknown proposal {proposal_id}")
        return next(i for i in self.items(row[0]) if i.proposal.proposal_id == proposal_id)

    # --- deciding -------------------------------------------------------------------
    def decide(self, proposal_id: str, decision: str, note: str = "",
               value: Any = None, at: datetime | None = None) -> list[Event]:
        item = self.get(proposal_id)
        p = item.proposal
        if item.status != "pending":
            raise QueueError(f"proposal {proposal_id} is {item.status}, not pending")
        if decision == "modified" and value is None:
            raise QueueError("modify needs a value")
        if decision in ("rejected", "modified") and not note.strip():
            raise QueueError(f"{decision} needs a coach note")
        at = at or datetime.now(timezone.utc)
        final = value if decision == "modified" else (p.proposed_value if decision == "approved" else None)
        events = [make_event(p.client_id, "proposal_decided", at, {
            "proposal_id": proposal_id, "decision": decision, "coach_note": note,
            "proposal": json.loads(p.model_dump_json()), "final_value": final}, Source.coach, at)]
        if decision != "rejected":
            events += self._effects(p, final, at)
        self.store.append(events)
        return events

    def _effects(self, p: Proposal, value: Any, at: datetime) -> list[Event]:
        """Events that make an approved change take effect. Volume, deload and
        adherence decisions are instructions to the client: the decision event
        itself is the record, and logged sessions show the outcome."""
        out: list[Event] = []
        if p.action in ("decrease_calories", "increase_calories"):
            macros = (value or {}).get("macros_by_day_type") if isinstance(value, dict) else None
            if macros:
                out.append(make_event(p.client_id, "nutrition_targets_set", at, {
                    "macros_by_day_type": {k: MacroTargets.model_validate(v) for k, v in macros.items()},
                    "proposal_id": p.proposal_id}, Source.coach, at))
        elif p.action == "transition_phase":
            v = value or {}
            if "planned_weeks" not in v:
                raise QueueError("phase transition needs planned_weeks: use modify with "
                                 '{"phase": ..., "planned_weeks": N, "target_rate_pct_bw": R}')
            out.append(make_event(p.client_id, "phase_started", datetime.combine(
                at.date(), time(0), tzinfo=timezone.utc), {
                "phase": v.get("phase", "maintenance"), "planned_weeks": v["planned_weeks"],
                "target_rate_pct_bw": v.get("target_rate_pct_bw", 0)}, Source.coach, at))
        return out

    def approved(self, client_id: str, since: date | None = None) -> list[Event]:
        """Approved/modified decisions (newest first) — what the LLM may present to clients."""
        out = [e for e in self._decisions(client_id).values()
               if e.payload.decision in ("approved", "modified")
               and (since is None or e.day >= since)]
        return sorted(out, key=lambda e: e.timestamp, reverse=True)
