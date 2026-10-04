"""Local dashboard: pages render from real engine state, queue actions write events,
chat is saved per client and Mr. J is framed as talking to the coach. No network."""
import io
import json
import shutil
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.store import ChatStore, find_clients
from ingest.onboarding import load_onboarding_config, onboard
from llm.client import Reply
from store.event_store import EventStore
from tests.test_m6_import_onboarding import SAMPLE

CID = "SYN-CUT-01"
AS_OF = "2026-09-28"


class FakeLLM:
    def __init__(self):
        self.systems, self.message_lists = [], []

    def respond(self, system, messages, tools, run_tool):
        self.systems.append(system)
        self.message_lists.append([dict(m) for m in messages])
        return Reply(f"reply #{len(self.systems)}", "end_turn")


@pytest.fixture()
def env(tmp_path, cfg):
    data = tmp_path / "data"
    folder = data / "syn"
    folder.mkdir(parents=True)
    onboard(EventStore(folder / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    llm = FakeLLM()
    client = TestClient(create_app(data, cfg, llm_factory=lambda: llm))
    return client, llm, folder / "coach.db"


def test_find_clients(env):
    _, _, db = env
    assert find_clients(db.parent.parent)[CID].db_path == db


def test_index_lists_client(env):
    client, _, _ = env
    r = client.get(f"/?as_of={AS_OF}")
    assert r.status_code == 200 and CID in r.text and "Coaching dashboard" in r.text
    assert "cloudfront.net" in r.text or "/static/splash.png" in r.text  # Higgsfield art


def test_client_page_shows_state_chart_and_review(env):
    client, _, _ = env
    r = client.get(f"/client/{CID}?as_of={AS_OF}")
    assert r.status_code == 200
    for needle in ("Cut · week 7", "<svg class=\"chart\"", "7-day average", "Table view",
                   "right shoulder (synthetic)", "History review", "Approval queue"):
        assert needle in r.text, needle
    data = json.loads(r.text.split('id="weight-data">')[1].split("</script>")[0])
    assert data and {"date", "weight", "avg"} <= set(data[0])


def test_unknown_client_404(env):
    client, _, _ = env
    assert client.get("/client/NOPE").status_code == 404


def test_refresh_and_approve_from_dashboard(env, cfg):
    client, _, db = env
    r = client.post(f"/client/{CID}/queue/refresh", data={"as_of": AS_OF}, follow_redirects=True)
    assert "Decrease calories" in r.text
    from approvals.queue import ApprovalQueue
    pid = ApprovalQueue(EventStore(db), cfg).pending(CID)[0].proposal.proposal_id
    r = client.post(f"/client/{CID}/proposal/{pid}/reject", data={"as_of": AS_OF, "note": ""},
                    follow_redirects=True)
    assert "reject failed" in r.text  # note required
    r = client.post(f"/client/{CID}/proposal/{pid}/approve", data={"as_of": AS_OF, "note": "start Monday"},
                    follow_redirects=True)
    assert "Nothing waiting for you" in r.text
    decided = [e for e in EventStore(db).read(CID) if e.type == "proposal_decided"]
    assert decided[0].payload.coach_note == "start Monday"


def test_modify_with_json_value(env, cfg):
    client, _, db = env
    client.post(f"/client/{CID}/queue/refresh", data={"as_of": AS_OF})
    from approvals.queue import ApprovalQueue
    pid = ApprovalQueue(EventStore(db), cfg).pending(CID)[0].proposal.proposal_id
    value = {"kcal_per_day_change": -50, "macros_by_day_type": {
        "moderate": {"protein_g": 190, "carb_g": 220, "fat_g": 64}}}
    r = client.post(f"/client/{CID}/proposal/{pid}/modify",
                    data={"as_of": AS_OF, "note": "half step", "value": json.dumps(value)}, follow_redirects=True)
    assert r.status_code == 200
    r = client.post(f"/client/{CID}/proposal/{pid}/modify", data={"as_of": AS_OF, "note": "x", "value": "{bad"},
                    follow_redirects=True)
    assert "failed" in r.text


def test_chat_is_saved_and_continues_with_coach_framing(env):
    client, llm, db = env
    r = client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "Why only 100 kcal?"},
                    follow_redirects=True)
    assert "Why only 100 kcal?" in r.text and "reply #1" in r.text
    assert "is this client's **coach**" in llm.systems[0]
    client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "OK, and cardio?"})
    # second call carries the saved history, append-only
    assert [m["content"] for m in llm.message_lists[1]] == ["Why only 100 kcal?", "reply #1", "OK, and cardio?"]
    assert [m["role"] for m in ChatStore(EventStore(db)).history(CID)] == ["user", "assistant"] * 2


def test_checkin_draft_marked_and_clear(env):
    client, llm, db = env
    r = client.post(f"/client/{CID}/checkin", data={"as_of": AS_OF}, follow_redirects=True)
    assert "Check-in draft" in r.text and "Copy" in r.text
    assert ChatStore(EventStore(db)).history(CID)[-1]["kind"] == "draft"
    client.post(f"/client/{CID}/chat/clear", data={"as_of": AS_OF})
    assert ChatStore(EventStore(db)).history(CID) == []


def test_llm_error_shown_not_saved(tmp_path, cfg, env):
    _, _, db = env

    class Broken:
        def respond(self, *a):
            raise RuntimeError("no API key")

    client = TestClient(create_app(db.parent.parent, cfg, llm_factory=lambda: Broken()))
    r = client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "hi"}, follow_redirects=True)
    assert "no API key" in r.text
    assert ChatStore(EventStore(db)).history(CID) == []


def test_onboard_upload(tmp_path, cfg):
    data = tmp_path / "data"
    client = TestClient(create_app(data, cfg, llm_factory=FakeLLM))
    files = [("files", (p.name, p.read_bytes())) for p in SAMPLE.iterdir() if p.suffix in (".csv", ".json")]
    r = client.post("/onboard", files=files)
    assert r.status_code == 200 and "Imported SYN-CUT-01" in r.text and "Weigh-ins: OK" in r.text
    assert (data / "syn-cut-01" / "coach.db").exists()
    assert CID in client.get("/").text


@pytest.mark.parametrize("name", ["../evil.csv", "run.sh", "notes.txt"])
def test_onboard_rejects_unsafe_files(tmp_path, cfg, name):
    client = TestClient(create_app(tmp_path / "data", cfg, llm_factory=FakeLLM))
    r = client.post("/onboard", files=[("files", (name, b"x"))])
    assert "not allowed" in r.text or "onboarding.json is required" in r.text
    assert not list((tmp_path / "data").rglob("*.sh")) and not (tmp_path / "evil.csv").exists()


def test_onboard_requires_onboarding_json(tmp_path, cfg):
    client = TestClient(create_app(tmp_path / "data", cfg, llm_factory=FakeLLM))
    r = client.post("/onboard", files=[("files", ("weigh_ins.csv", b"Date,W\n"))])
    assert "onboarding.json is required" in r.text
