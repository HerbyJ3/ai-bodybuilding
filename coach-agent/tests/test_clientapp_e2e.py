"""End-to-end: the client app and the coach dashboard running against ONE data folder
(BUILD_SPEC §15). What the client logs reaches the coach; support messages flow both ways;
pending proposals and the coach's Mr. J chat never reach the client; the client's Mr. J chat
never enters the coach's Mr. J session. Synthetic sample client only, fake LLM, no network."""
import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from approvals.queue import ApprovalQueue
from clientapp.app import create_client_app
from dashboard.app import create_app
from dashboard.store import ChatStore, SupportStore
from engine.state_builder import build_state
from ingest.onboarding import load_onboarding_config, onboard
from schemas.events import Source
from store.event_store import EventStore
from tests.test_dashboard import CID, FakeLLM
from tests.test_m6_import_onboarding import SAMPLE

TODAY = date(2026, 9, 28)
AS_OF = TODAY.isoformat()


class Both:
    """Client app + admin dashboard on the same data dir; every client response is recorded."""

    def __init__(self, data, db, cfg):
        self.db = db
        self.cfg = cfg
        self.client_llm, self.coach_llm = FakeLLM(), FakeLLM()
        self.app = TestClient(create_client_app(data, cfg, llm_factory=lambda: self.client_llm,
                                                today=lambda: TODAY), follow_redirects=False)
        self.admin = TestClient(create_app(data, cfg, llm_factory=lambda: self.coach_llm),
                                follow_redirects=False)
        self.seen: list[str] = []  # every body + header the client app returned

    # --- client side --------------------------------------------------------------------
    def get(self, path: str) -> str:
        r = self.app.get(path)
        assert r.status_code == 200, (path, r.status_code)
        self.seen.append(r.text)
        return r.text

    def post(self, route: str, data: dict) -> tuple[str, str]:
        """POST, then follow the redirect like a browser. Returns (location, page html)."""
        r = self.app.post(f"/c/{CID}/{route}", data=data)
        assert r.status_code == 303, r.text
        loc = r.headers["location"]
        self.seen += [loc, r.text, json.dumps(dict(r.headers))]
        return loc, self.get(loc.split("#")[0])

    def visit_everything(self) -> None:
        self.get("/")
        self.get(f"/c/{CID}")

    # --- coach side ----------------------------------------------------------------------
    def page(self) -> str:
        r = self.admin.get(f"/client/{CID}?as_of={AS_OF}")
        assert r.status_code == 200
        return r.text

    def index(self) -> str:
        return self.admin.get(f"/?as_of={AS_OF}").text

    def events(self):
        return EventStore(self.db).read(CID)


@pytest.fixture()
def both(tmp_path, cfg):
    data = tmp_path / "data"
    folder = data / "syn"
    folder.mkdir(parents=True)
    onboard(EventStore(folder / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    return Both(data, folder / "coach.db", cfg)


def section(html: str, sid: str) -> str:
    return html.split(f'id="{sid}"')[1].split("</section>")[0]


def weight_data(html: str) -> list[dict]:
    m = re.search(r'<script type="application/json" id="weight-data">(.*?)</script>', html, re.S)
    assert m, "no weight-data on page"
    return json.loads(m.group(1))


# --- client logs reach the coach --------------------------------------------------------------

def test_client_logs_show_in_admin_page_chart_and_state(both):
    before = {e.event_id for e in both.events()}
    assert "2026-09-28" not in [p["date"] for p in weight_data(both.page())]

    loc, html = both.post("weigh-in", {"weight": "188.3", "unit": "lb", "conditions": "fasted"})
    assert "notice=" in loc and "Weigh-in saved: 188.3 lb" in html
    loc, html = both.post("daily-target", {"hit": "no", "off_carbs": "-30", "off_fat": "10"})
    assert "notice=" in loc and "Daily target saved" in html
    loc, html = both.post("checkin", {"hunger": "high", "energy": "low", "training_feel": "good",
                                      "sleep_hours": "6", "notes": "SYNTH-CI-NOTE"})
    assert "notice=" in loc and "Check-in saved" in html

    # build_state (what the engine sees)
    st = build_state(both.events(), TODAY, both.cfg)
    assert any(p.day == TODAY and p.payload.weight == 188.3 for p in both.events() if p.type == "weigh_in")
    today_ci = [c for c in st.checkins_recent if c["date"] == TODAY]
    assert len(today_ci) == 1 and today_ci[0]["notes"] == "SYNTH-CI-NOTE"
    assert st.latest_checkin and st.latest_checkin["date"] == TODAY
    day = [r for r in st.macro_adherence["recent"] if r["date"] == AS_OF]
    assert len(day) == 1 and not day[0]["hit"]
    assert day[0]["off_by"] == {"carb_g": -30, "fat_g": 10}

    # the coach's client page: chart data, chart table, daily-target log, check-in table
    page = both.page()
    assert {"date": AS_OF, "weight": 188.3} in [{k: p[k] for k in ("date", "weight")}
                                                for p in weight_data(page)]
    assert "<td>2026-09-28</td><td>188.3</td>" in page
    assert "2026-09-28: missed: carbs -30 g, fat +10 g" in page
    checkins = section(page, "checkins")
    assert "Saved for this date" in checkins
    assert "<tr><td>2026-09-28</td><td>high</td><td>low</td><td>good</td><td>6 h</td></tr>" in checkins

    # the client sees the same reading in their own chart
    assert {"date": AS_OF, "weight": 188.3} in [{k: p[k] for k in ("date", "weight")}
                                                for p in weight_data(both.get(f"/c/{CID}"))]

    # every event the client app created is Source.client
    new = [e for e in both.events() if e.event_id not in before]
    assert sorted(e.type for e in new) == ["macro_adherence_logged", "weekly_checkin", "weigh_in"]
    assert all(e.source == Source.client for e in new), [(e.type, e.source) for e in new]


def test_all_client_created_events_are_source_client(both):
    """Every write route of the client app, including a same-day replacement and a past date."""
    before = {e.event_id for e in both.events()}
    both.post("weigh-in", {"weight": "188.3", "unit": "lb"})
    both.post("weigh-in", {"weight": "85.5", "unit": "kg", "when": "2026-09-27"})
    both.post("daily-target", {"hit": "yes"})
    both.post("daily-target", {"hit": "no", "off_protein": "-20", "non_training": "1",
                               "when": "2026-09-26"})
    both.post("checkin", {"hunger": "mid", "energy": "mid"})
    both.post("checkin", {"hunger": "low", "energy": "high"})  # replaces today's
    both.post("chat", {"message": "What are my targets?"})
    both.post("support", {"message": "Can we talk?"})
    new = [e for e in both.events() if e.event_id not in before]
    assert len(new) == 6
    assert {e.source for e in new} == {Source.client}


# --- support thread ------------------------------------------------------------------------------

def test_support_round_trip_unread_seen_and_reply(both):
    loc, html = both.post("support", {"message": "SYNTH-SUPPORT-Q1"})
    assert "notice=" in loc and "Message sent to support" in html
    assert "Seen" not in section(html, "support")

    # coach: unread on the client page and the index badge; GETs don't mark it read
    card = section(both.page(), "support")
    assert "SYNTH-SUPPORT-Q1" in card and '<span class="pill unread">1 new</span>' in card
    assert f'action="/client/{CID}/support/read"' in card
    assert '<span class="pill unread">1 new message</span>' in both.index()
    assert SupportStore(EventStore(both.db)).unread_for_coach(CID) == 1

    # coach marks read: unread clears everywhere and the client sees "Seen"
    r = both.admin.post(f"/client/{CID}/support/read", data={"as_of": AS_OF})
    assert r.status_code == 303
    card = section(both.page(), "support")
    assert 'class="pill unread"' not in card and "/support/read" not in card
    assert 'class="pill unread"' not in both.index()
    client_card = section(both.get(f"/c/{CID}"), "support")
    assert "SYNTH-SUPPORT-Q1" in client_card and "· Seen" in client_card

    # a second message is unread again; the coach's reply shows in the client app
    both.post("support", {"message": "SYNTH-SUPPORT-Q2"})
    assert '<span class="pill unread">1 new message</span>' in both.index()
    r = both.admin.post(f"/client/{CID}/support/reply",
                        data={"as_of": AS_OF, "message": "SYNTH-COACH-REPLY"})
    assert r.status_code == 303 and "error=" not in r.headers["location"]
    assert 'class="pill unread"' not in both.index()
    client_card = section(both.get(f"/c/{CID}"), "support")
    order = [client_card.index(s) for s in ("SYNTH-SUPPORT-Q1", "SYNTH-SUPPORT-Q2", "SYNTH-COACH-REPLY")]
    assert order == sorted(order)
    assert client_card.count("· Seen") == 2  # both client messages; never on the coach's own reply
    assert [m["sender"] for m in SupportStore(EventStore(both.db)).thread(CID)] == ["client", "client", "coach"]
    assert both.client_llm.systems == [] and both.coach_llm.systems == []  # no AI in this thread


# --- what must never reach the client --------------------------------------------------------

PENDING = "SYNTH-PENDING-SENTINEL"


def _plant_pending_sentinel(both) -> str:
    q = ApprovalQueue(EventStore(both.db), both.cfg)
    q.refresh(CID, TODAY)
    item = q.pending(CID)[0]
    body = item.proposal.model_dump(mode="json")
    body["rationale_short"] = f"{PENDING} rationale"
    body["inputs_used"] = {**(body.get("inputs_used") or {}), "note": f"{PENDING} input"}
    db = EventStore(both.db)._db
    with db:
        db.execute("UPDATE proposals SET body = ? WHERE proposal_id = ?",
                   (json.dumps(body), item.proposal.proposal_id))
    assert [i.proposal.proposal_id for i in ApprovalQueue(EventStore(both.db), both.cfg).pending(CID)
            if PENDING in i.proposal.rationale_short] == [item.proposal.proposal_id]
    return item.proposal.proposal_id


def test_pending_proposal_never_reaches_client(both):
    pid = _plant_pending_sentinel(both)
    assert PENDING in both.page()  # sanity: the coach does see it

    both.visit_everything()
    both.post("weigh-in", {"weight": "188.3", "unit": "lb"})
    both.post("weigh-in", {"weight": "abc", "unit": "lb"})  # error path too
    both.post("daily-target", {"hit": "yes"})
    both.post("daily-target", {"hit": "no"})
    both.post("checkin", {"hunger": "mid", "energy": "mid"})
    both.post("checkin", {"hunger": "starving", "energy": "mid"})
    both.post("chat", {"message": "Is anything about to change in my plan?"})
    both.post("chat", {"message": ""})
    both.post("support", {"message": "hello"})
    both.post("support", {"message": ""})
    both.visit_everything()

    assert len(both.seen) > 20
    for text in both.seen:
        assert PENDING not in text and pid not in text
    # nor in what Mr. J is told in the client's session
    assert both.client_llm.systems
    for system in both.client_llm.systems:
        assert PENDING not in system and pid not in system
    for msgs in both.client_llm.message_lists:
        assert PENDING not in str(msgs)


# --- the two Mr. J chats stay apart -----------------------------------------------------------

COACH_SECRET = "SYNTH-COACH-MRJ-SENTINEL"
CLIENT_SECRET = "SYNTH-CLIENT-MRJ-SENTINEL"


def test_coach_and_client_mr_j_chats_never_mix(both):
    # the coach chats with Mr. J in the dashboard and drafts a check-in
    r = both.admin.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": COACH_SECRET})
    assert r.status_code == 303 and "error=" not in r.headers["location"]
    both.admin.post(f"/client/{CID}/checkin", data={"as_of": AS_OF})
    assert [m["content"] for m in ChatStore(EventStore(both.db), "coach").history(CID)][0] == COACH_SECRET

    # the client chats with Mr. J in the client app
    both.post("chat", {"message": CLIENT_SECRET})
    both.post("chat", {"message": "And what about tomorrow?"})
    both.visit_everything()

    # client side: the coach's chat is never shown nor sent to the client's Mr. J
    for text in both.seen:
        assert COACH_SECRET not in text
    assert all(COACH_SECRET not in str(ms) for ms in both.client_llm.message_lists)
    assert all(COACH_SECRET not in s for s in both.client_llm.systems)
    assert CLIENT_SECRET in section(both.get(f"/c/{CID}"), "chat")

    # coach side: the client's chat never enters the coach's Mr. J session
    both.admin.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "follow-up"})
    assert both.coach_llm.message_lists
    assert all(CLIENT_SECRET not in str(ms) for ms in both.coach_llm.message_lists)
    assert all(CLIENT_SECRET not in s for s in both.coach_llm.systems)
    assert all(CLIENT_SECRET not in m["content"]
               for m in ChatStore(EventStore(both.db), "coach").history(CID))
    page = both.page()
    assert CLIENT_SECRET not in section(page, "chat")  # the coach's own Mr. J chat card
    # ...but the coach can read it in the read-only "Client's chat with Mr. J" (intended)
    client_chat = page.split('class="client-chat"')[1].split("</details>")[0]
    assert CLIENT_SECRET in client_chat and "<form" not in client_chat

    # clearing the coach's chat leaves the client's chat alone, and vice versa nothing leaks
    both.admin.post(f"/client/{CID}/chat/clear", data={"as_of": AS_OF})
    assert CLIENT_SECRET in section(both.get(f"/c/{CID}"), "chat")
    assert ChatStore(EventStore(both.db), "coach").history(CID) == []


# --- error messages are readable in both apps -------------------------------------------------

def test_validation_errors_are_readable_not_a_link(both):
    """A schema-level rejection (e.g. weight <= 0) must show the reason, not pydantic's
    'For further information visit https://errors.pydantic.dev/...' footer line."""
    from urllib.parse import parse_qs, urlsplit

    def error_of(loc: str) -> str:
        return parse_qs(urlsplit(loc).query)["error"][0]

    loc, html = both.post("weigh-in", {"weight": "-5", "unit": "lb"})
    msg = error_of(loc)
    assert msg.startswith("Weigh-in not saved: ") and "greater than 0" in msg, msg
    assert "pydantic" not in msg and "http" not in msg and "pydantic" not in html
    r = both.admin.post(f"/client/{CID}/weigh-in", data={"as_of": AS_OF, "weight": "-5", "unit": "lb"})
    msg = error_of(r.headers["location"])
    assert "greater than 0" in msg and "pydantic" not in msg and "http" not in msg, msg
