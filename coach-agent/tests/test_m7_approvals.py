from datetime import date, datetime, timezone

import pytest

from approvals.queue import ApprovalQueue, QueueError
from engine.state_builder import build_state
from ingest.onboarding import load_onboarding_config, onboard
from store.event_store import EventStore
from tests.test_m6_import_onboarding import SAMPLE

CID = "SYN-CUT-01"
AS_OF = date(2026, 9, 28)
AT = datetime(2026, 9, 28, 18, tzinfo=timezone.utc)


@pytest.fixture()
def queue(cfg):
    store = EventStore()
    onboard(store, load_onboarding_config(SAMPLE / "onboarding.json"), cfg, base_dir=SAMPLE)
    q = ApprovalQueue(store, cfg)
    q.refresh(CID, AS_OF)
    return q


def _calorie_item(q):
    return next(i for i in q.pending(CID) if i.proposal.target == "calories.cut")


def test_refresh_lists_actionable_pending_and_holds_as_info(queue):
    statuses = {i.proposal.target: i.status for i in queue.items(CID)}
    assert statuses["calories.cut"] == "pending"
    assert statuses["volume.chest"] == "info"  # cold-start hold: nothing to approve
    assert [i.proposal.target for i in queue.pending(CID)] == ["calories.cut"]


def test_refresh_is_idempotent(queue):
    before = len(queue.items(CID))
    queue.refresh(CID, AS_OF)
    assert len(queue.items(CID)) == before


def test_refresh_requires_consent(cfg):
    with pytest.raises(QueueError, match="consent"):
        ApprovalQueue(EventStore(), cfg).refresh("nobody", AS_OF)


def test_approve_logs_decision_and_applies_macros(queue, cfg):
    item = _calorie_item(queue)
    queue.decide(item.proposal.proposal_id, "approved", at=AT)
    events = queue.store.read(CID)
    [dec] = [e for e in events if e.type == "proposal_decided"]
    assert dec.source.value == "coach" and dec.payload.decision == "approved"
    assert dec.payload.proposal["rule_id"] == "nutrition.weekly_adjustment"
    assert dec.payload.final_value == item.proposal.proposed_value
    targets = [e for e in events if e.type == "nutrition_targets_set"]
    assert targets[-1].payload.proposal_id == item.proposal.proposal_id
    st = build_state(events, AS_OF, cfg)
    expected = item.proposal.proposed_value["macros_by_day_type"]["moderate"]
    assert st.current_macros["moderate"].model_dump() == expected
    assert queue.get(item.proposal.proposal_id).status == "approved"
    assert queue.pending(CID) == []


def test_reject_needs_note_and_changes_nothing(queue):
    pid = _calorie_item(queue).proposal.proposal_id
    with pytest.raises(QueueError, match="note"):
        queue.decide(pid, "rejected")
    n = len(queue.store.read(CID))
    queue.decide(pid, "rejected", "client travelling this week", at=AT)
    events = queue.store.read(CID)
    [dec] = [e for e in events if e.type == "proposal_decided"]
    assert len(events) == n + 1 and dec.payload.decision == "rejected"


def test_modify_uses_coach_value(queue, cfg):
    pid = _calorie_item(queue).proposal.proposal_id
    value = {"kcal_per_day_change": -250, "macros_by_day_type": {
        "moderate": {"protein_g": 190, "carb_g": 200, "fat_g": 60}}}
    with pytest.raises(QueueError, match="value"):
        queue.decide(pid, "modified", "smaller step")
    queue.decide(pid, "modified", "smaller step while hunger is high", value, at=AT)
    st = build_state(queue.store.read(CID), AS_OF, cfg)
    assert st.current_macros["moderate"].carb_g == 200
    assert queue.approved(CID)[0].payload.final_value == value


def test_cannot_decide_twice(queue):
    pid = _calorie_item(queue).proposal.proposal_id
    queue.decide(pid, "approved", at=AT)
    with pytest.raises(QueueError, match="not pending"):
        queue.decide(pid, "rejected", "changed my mind", at=AT)


def test_newer_proposal_supersedes_older(queue):
    old = _calorie_item(queue).proposal.proposal_id
    queue.refresh(CID, date(2026, 10, 3))
    assert queue.get(old).status == "superseded"
    with pytest.raises(QueueError):
        queue.decide(old, "approved", at=AT)


def test_phase_transition_needs_planned_weeks(cfg):
    from schemas.proposals import new_proposal
    store = EventStore()
    q = ApprovalQueue(store, cfg)
    p = new_proposal(CID, AS_OF, "nutrition.phase_transition", "phase", "transition_phase", "x",
                     proposed_value={"phase": "maintenance"})
    store._db.execute("INSERT INTO proposals VALUES (?,?,?,?,?)",
                      (p.proposal_id, CID, p.created_at.isoformat(), p.target, p.model_dump_json()))
    with pytest.raises(QueueError, match="planned_weeks"):
        q.decide(p.proposal_id, "approved", at=AT)
    assert store.read(CID) == []  # nothing written on failure
    q.decide(p.proposal_id, "modified", "8 weeks maintenance",
             {"phase": "maintenance", "planned_weeks": 8, "target_rate_pct_bw": 0}, at=AT)
    [ph] = [e for e in store.read(CID) if e.type == "phase_started"]
    assert ph.payload.planned_weeks == 8 and ph.source.value == "coach"


def test_cli_queue_flow(tmp_path):
    from typer.testing import CliRunner
    from ingest.cli import app
    db = str(tmp_path / "c.db")
    r = CliRunner()
    assert r.invoke(app, ["onboard", str(SAMPLE / "onboarding.json"), "--db", db]).exit_code == 0
    out = r.invoke(app, ["queue", "refresh", CID, "2026-09-28", "--db", db]).output
    assert "decrease_calories" in out
    pid = out.split()[0]
    assert r.invoke(app, ["queue", "show", pid, "--db", db]).exit_code == 0
    assert r.invoke(app, ["queue", "reject", pid, "--db", db]).exit_code != 0  # note required
    assert r.invoke(app, ["queue", "approve", pid, "--db", db]).exit_code == 0
    assert "nothing pending" in r.invoke(app, ["queue", "list", CID, "--db", db]).output
    assert "approved" in r.invoke(app, ["queue", "list", CID, "--all", "--db", db]).output
