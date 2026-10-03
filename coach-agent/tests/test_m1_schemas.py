from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from schemas.events import Event, Source, make_event
from schemas.proposals import new_proposal


def test_payload_is_typed():
    e = make_event("c", "weigh_in", date(2026, 1, 1), {"weight": "190.5", "unit": "lb"})
    assert e.payload.weight == 190.5 and e.source is Source.client


@pytest.mark.parametrize("etype,payload", [
    ("soreness_rated", {"muscle": "chest", "soreness": 4}),
    ("stimulus_rated", {"session_id": "s", "muscle": "chest", "mind_muscle": -1, "pump": 0, "disruption": 0}),
    ("weekly_checkin", {"adherence_pct": 90, "hunger": 0, "energy": 3, "sleep": 3}),
    ("weigh_in", {"weight": 0, "unit": "lb"}),
    ("weigh_in", {"weight": 190, "unit": "stone"}),
    ("phase_started", {"phase": "bulk", "target_rate_pct_bw": 0.5, "planned_weeks": 8}),
    ("cardio_logged", {"date": "2026-01-01", "modality": "bike", "minutes": 20, "intensity": "extreme"}),
    ("deload_completed", {"start_date": "2026-01-07", "end_date": "2026-01-01"}),
    ("set_logged", {"exercise_id": "x", "muscle": "m", "load": 1, "reps": 1, "rir": 1,
                    "set_index": 0, "session_id": "s", "extra": 1}),
])
def test_invalid_payloads_rejected(etype, payload):
    with pytest.raises(ValidationError):
        make_event("c", etype, date(2026, 1, 1), payload)


def test_unknown_event_type_rejected():
    with pytest.raises((ValidationError, KeyError)):
        make_event("c", "made_up", date(2026, 1, 1), {})


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        Event(event_id="x", client_id="c", type="weigh_in", timestamp=datetime(2026, 1, 1),
              source="client", recorded_at=datetime.now(timezone.utc),
              payload={"weight": 1, "unit": "lb"})


def test_event_id_is_content_derived():
    a = make_event("c", "weigh_in", date(2026, 1, 1), {"weight": 190, "unit": "lb"}, "import")
    b = make_event("c", "weigh_in", date(2026, 1, 1), {"weight": 190.0, "unit": "lb"}, "import")
    c = make_event("c", "weigh_in", date(2026, 1, 2), {"weight": 190, "unit": "lb"}, "import")
    assert a.event_id == b.event_id != c.event_id


def test_import_source():
    e = make_event("c", "weigh_in", date(2026, 1, 1), {"weight": 190, "unit": "lb"}, "import")
    assert e.source is Source.import_


def test_proposal_ids_deterministic():
    p1 = new_proposal("c", date(2026, 1, 1), "r", "t", "hold", "x")
    p2 = new_proposal("c", date(2026, 1, 1), "r", "t", "hold", "y")
    assert p1.proposal_id == p2.proposal_id and p1.status == "pending"
