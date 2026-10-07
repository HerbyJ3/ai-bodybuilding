"""Client app prototype (BUILD_SPEC §15): its own FastAPI app with no admin routes, dated by the
server's today, Source.client on every event, and only approved changes shown. Synthetic client
only, fake LLM, no network."""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from approvals.queue import ApprovalQueue
from clientapp.app import create_client_app
from dashboard.store import ChatStore, SupportStore
from engine.state_builder import build_state
from ingest.onboarding import load_onboarding_config, onboard
from schemas.events import Source
from store.event_store import EventStore
from tests.test_dashboard import CID, FakeLLM
from tests.test_m6_import_onboarding import SAMPLE

TODAY = date(2026, 9, 28)


@pytest.fixture()
def env(tmp_path, cfg):
    data = tmp_path / "data"
    folder = data / "syn"
    folder.mkdir(parents=True)
    onboard(EventStore(folder / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    llm = FakeLLM()
    app = create_client_app(data, cfg, llm_factory=lambda: llm, today=lambda: TODAY)
    return TestClient(app, follow_redirects=False), llm, folder / "coach.db"


def new_events(db, before, type_):
    return [e for e in EventStore(db).read(CID) if e.type == type_ and e.event_id not in before]


def ids(db):
    return {e.event_id for e in EventStore(db).read(CID)}


def post(client, path, data, anchor):
    r = client.post(f"/c/{CID}/{path}", data=data)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith(f"/c/{CID}") and loc.endswith(f"#{anchor}"), loc
    return loc


# --- pages --------------------------------------------------------------------------------

def test_pick_lists_clients(env):
    client, _, _ = env
    r = client.get("/")
    assert r.status_code == 200 and f'href="/c/{CID}"' in r.text


def test_home_renders_all_sections(env):
    client, _, _ = env
    r = client.get(f"/c/{CID}")
    assert r.status_code == 200
    for anchor in ("weigh-in", "daily-target", "checkin", "targets", "chart", "chat", "support"):
        assert f'id="{anchor}"' in r.text, anchor
    assert '<svg class="chart"' in r.text and 'id="weight-data"' in r.text
    assert 'name="non_training"' in r.text and "2026-09-28" in r.text
    checkin_form = r.text.split('action="/c/SYN-CUT-01/checkin"')[1].split("</form>")[0]
    assert 'name="hunger"' in checkin_form and 'type="date"' not in checkin_form
    assert 'name="when"' not in checkin_form and "as_of" not in r.text
    assert "190 g" in r.text  # current protein target from the sample


def test_unknown_client_404_everywhere(env):
    client, _, _ = env
    assert client.get("/c/NOPE").status_code == 404
    for path in ("weigh-in", "daily-target", "checkin", "chat", "support"):
        assert client.post(f"/c/NOPE/{path}", data={"message": "x"}).status_code == 404, path


def test_no_admin_routes(env):
    client, _, _ = env
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert not any(p.startswith(("/client", "/onboard")) for p in paths), paths
    assert client.get(f"/client/{CID}").status_code == 404
    assert client.get("/onboard").status_code == 404
    assert client.post(f"/client/{CID}/queue/refresh").status_code in (404, 405)


def test_static_mounts(env):
    client, _, _ = env
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/app-static/client.css").status_code == 200


# --- what the client may see ----------------------------------------------------------

def test_pending_proposal_not_shown(env, cfg):
    client, _, db = env
    ApprovalQueue(EventStore(db), cfg).refresh(CID, TODAY)
    pending = ApprovalQueue(EventStore(db), cfg).pending(CID)
    assert pending
    text = client.get(f"/c/{CID}").text
    for item in pending:
        assert item.proposal.proposal_id not in text
        assert item.proposal.rationale_short not in text
    assert "decrease calories" not in text.lower() and "cold start" not in text


def test_approved_change_shown_without_coach_note(env, cfg):
    client, _, db = env
    q = ApprovalQueue(EventStore(db), cfg)
    q.refresh(CID, TODAY)
    pid = q.pending(CID)[0].proposal.proposal_id
    q.decide(pid, "approved", note="SYNTH-PRIVATE-NOTE", at=datetime(2026, 9, 28, 9, tzinfo=timezone.utc))
    text = client.get(f"/c/{CID}").text
    assert "decrease calories" in text.lower()
    assert "SYNTH-PRIVATE-NOTE" not in text and pid not in text


def test_future_approved_change_hidden(env, cfg):
    client, _, db = env
    q = ApprovalQueue(EventStore(db), cfg)
    q.refresh(CID, TODAY)
    pid = q.pending(CID)[0].proposal.proposal_id
    q.decide(pid, "approved", at=datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    assert "decrease calories" not in client.get(f"/c/{CID}").text.lower()


def test_coach_chat_and_support_isolation_on_page(env):
    client, _, db = env
    ChatStore(EventStore(db), "coach").add(CID, "user", "SYNTH-COACH-ONLY")
    SupportStore(EventStore(db)).add(CID, "coach", "SYNTH-COACH-REPLY")
    text = client.get(f"/c/{CID}").text
    assert "SYNTH-COACH-ONLY" not in text and "SYNTH-COACH-REPLY" in text


# --- logging --------------------------------------------------------------------------

def test_weigh_in_defaults_to_today_source_client(env):
    client, _, db = env
    before = ids(db)
    loc = post(client, "weigh-in", {"weight": "185.4", "unit": "lb", "conditions": "fasted"}, "weigh-in")
    assert "notice=" in loc
    (ev,) = new_events(db, before, "weigh_in")
    assert ev.day == TODAY and ev.source == Source.client and ev.payload.weight == 185.4


def test_weigh_in_past_date_ok_future_and_bad_rejected(env):
    client, _, db = env
    before = ids(db)
    post(client, "weigh-in", {"weight": "185", "unit": "lb", "when": "2026-09-27"}, "weigh-in")
    assert [e.day for e in new_events(db, before, "weigh_in")] == [date(2026, 9, 27)]
    before = ids(db)
    for data in ({"weight": "185", "unit": "lb", "when": "2026-09-29"},
                 {"weight": "185", "unit": "lb", "when": "not-a-date"},
                 {"weight": "nan", "unit": "lb"}, {"weight": "inf", "unit": "lb"},
                 {"weight": "abc", "unit": "lb"}, {"weight": "-5", "unit": "lb"},
                 {"weight": "185", "unit": "stone"}):
        assert "error=" in post(client, "weigh-in", data, "weigh-in"), data
    assert new_events(db, before, "weigh_in") == []


def test_daily_target_hit_and_missed(env, cfg):
    client, _, db = env
    before = ids(db)
    post(client, "daily-target", {"hit": "yes"}, "daily-target")
    (ev,) = new_events(db, before, "macro_adherence_logged")
    assert ev.source == Source.client and ev.payload.date == TODAY and ev.payload.hit
    assert ev.payload.day_type == "moderate"  # training-day targets by default
    before = ids(db)
    post(client, "daily-target", {"hit": "no", "off_carbs": "-30", "non_training": "1",
                                  "when": "2026-09-27"}, "daily-target")
    (ev,) = new_events(db, before, "macro_adherence_logged")
    assert ev.payload.date == date(2026, 9, 27) and not ev.payload.hit
    assert ev.payload.off_by == {"carb_g": -30} and ev.payload.day_type == "non_training"


def test_daily_target_rejects_bad_input(env):
    client, _, db = env
    before = ids(db)
    for data in ({"hit": "no"}, {"hit": "yes", "when": "2026-09-30"}, {"hit": "no", "off_fat": "nan"}):
        assert "error=" in post(client, "daily-target", data, "daily-target"), data
    assert new_events(db, before, "macro_adherence_logged") == []


def test_checkin_dated_today_and_replaced_same_day(env, cfg):
    client, _, db = env
    before = ids(db)
    post(client, "checkin", {"hunger": "high", "energy": "low", "sleep_hours": "6",
                             "training_feel": "good", "when": "2026-09-01"}, "checkin")
    (ev,) = new_events(db, before, "weekly_checkin")
    assert ev.day == TODAY and ev.source == Source.client  # a stray date field is ignored
    post(client, "checkin", {"hunger": "mid", "energy": "high", "notes": "better"}, "checkin")
    st = build_state(EventStore(db).read(CID), TODAY, cfg)
    today_ci = [c for c in st.checkins_recent if c["date"] == TODAY]
    assert len(today_ci) == 1 and today_ci[0]["hunger"] == 3 and today_ci[0]["notes"] == "better"
    assert "Saved for today" in client.get(f"/c/{CID}").text


def test_checkin_rejects_bad_levels(env):
    client, _, db = env
    before = ids(db)
    assert "error=" in post(client, "checkin", {"hunger": "starving", "energy": "mid"}, "checkin")
    assert "error=" in post(client, "checkin", {"hunger": "mid", "energy": "mid", "sleep_hours": "x"},
                            "checkin")
    assert new_events(db, before, "weekly_checkin") == []


# --- Mr. J chat and support ------------------------------------------------------------

def test_client_chat_saved_in_client_audience_with_history(env):
    client, llm, db = env
    ChatStore(EventStore(db), "coach").add(CID, "user", "SYNTH-COACH-ONLY")
    post(client, "chat", {"message": "What are my targets?"}, "chat")
    post(client, "chat", {"message": "And tomorrow?"}, "chat")
    hist = ChatStore(EventStore(db), "client").history(CID)
    assert [m["content"] for m in hist] == ["What are my targets?", "reply #1", "And tomorrow?", "reply #2"]
    assert "What are my targets?" in str(llm.message_lists[-1])
    assert all("SYNTH-COACH-ONLY" not in str(m) for ms in llm.message_lists for m in ms)
    assert [m["content"] for m in ChatStore(EventStore(db), "coach").history(CID)] == ["SYNTH-COACH-ONLY"]
    assert "reply #2" in client.get(f"/c/{CID}").text


def test_client_chat_uses_client_app_audience(env, monkeypatch):
    import llm.coach
    client, _, _ = env
    seen = {}
    real = llm.coach.open_session

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)
    monkeypatch.setattr(llm.coach, "open_session", spy)
    post(client, "chat", {"message": "hi"}, "chat")
    assert seen["audience"] == "client_app" and seen["history"] == []


def test_client_chat_rejects_empty_and_long(env):
    client, llm, db = env
    for msg in ("", "   ", "x" * 4001):
        assert "error=" in post(client, "chat", {"message": msg}, "chat")
    assert llm.systems == [] and ChatStore(EventStore(db), "client").history(CID) == []


def test_client_chat_error_shown_nothing_stored(tmp_path, cfg, env):
    _, _, db = env

    def boom():
        raise RuntimeError("no key")
    app = create_client_app(db.parent.parent, cfg, llm_factory=boom, today=lambda: TODAY)
    client = TestClient(app, follow_redirects=False)
    loc = post(client, "chat", {"message": "hi"}, "chat")
    assert "error=" in loc
    assert ChatStore(EventStore(db), "client").history(CID) == []


def test_support_message_from_client(env):
    client, llm, db = env
    loc = post(client, "support", {"message": "  Can we talk about my plan?  "}, "support")
    assert "notice=" in loc
    s = SupportStore(EventStore(db))
    assert [(m["sender"], m["content"]) for m in s.thread(CID)] == [("client", "Can we talk about my plan?")]
    assert s.unread_for_coach(CID) == 1 and llm.systems == []  # no AI in this thread
    assert "Can we talk about my plan?" in client.get(f"/c/{CID}").text
    for msg in ("", "x" * 4001):
        assert "error=" in post(client, "support", {"message": msg}, "support")
    assert len(SupportStore(EventStore(db)).thread(CID)) == 1
