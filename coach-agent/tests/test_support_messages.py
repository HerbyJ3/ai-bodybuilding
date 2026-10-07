"""Chat audiences (coach vs client) stay apart, old chat tables migrate, and the coach <-> client
support thread works from the admin dashboard. Synthetic client only, no network."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.store import ChatStore, SupportStore
from ingest.onboarding import load_onboarding_config, onboard
from store.event_store import EventStore
from tests.test_dashboard import AS_OF, CID, FakeLLM
from tests.test_m6_import_onboarding import SAMPLE


@pytest.fixture()
def env(tmp_path, cfg):
    data = tmp_path / "data"
    folder = data / "syn"
    folder.mkdir(parents=True)
    onboard(EventStore(folder / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    llm = FakeLLM()
    return TestClient(create_app(data, cfg, llm_factory=lambda: llm), follow_redirects=False), llm, folder / "coach.db"


def test_audiences_are_separate(tmp_path):
    store = EventStore(tmp_path / "x.db")
    coach, client = ChatStore(store, "coach"), ChatStore(store, "client")
    coach.add(CID, "user", "coach question")
    client.add(CID, "user", "client question")
    assert [m["content"] for m in coach.history(CID)] == ["coach question"]
    assert [m["content"] for m in client.history(CID)] == ["client question"]
    client.clear(CID)
    assert client.history(CID) == [] and len(coach.history(CID)) == 1
    assert ChatStore(store).audience == "coach"  # default keeps old callers on the coach chat


def test_unknown_audience_rejected(tmp_path):
    with pytest.raises(ValueError):
        ChatStore(EventStore(tmp_path / "x.db"), "admin")


def test_old_chat_table_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    EventStore(db)
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, created_at TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), kind TEXT NOT NULL DEFAULT 'chat',
        content TEXT NOT NULL);
        INSERT INTO chat_messages (client_id, created_at, role, kind, content)
        VALUES ('SYN-CUT-01', '2026-09-01T00:00:00+00:00', 'user', 'chat', 'old coach line');""")
    con.commit()
    con.close()
    store = EventStore(db)
    assert ChatStore(store, "client").history(CID) == []
    assert [m["content"] for m in ChatStore(store, "coach").history(CID)] == ["old coach line"]
    ChatStore(store, "client")  # second open: migration is idempotent
    cols = [r[1] for r in store._db.execute("PRAGMA table_info(chat_messages)")]
    assert cols.count("audience") == 1


def test_support_store_thread_and_unread(tmp_path):
    s = SupportStore(EventStore(tmp_path / "x.db"))
    s.add(CID, "client", "hello coach")
    s.add(CID, "client", "second")
    s.add(CID, "coach", "hi")
    s.add("SYN-OTHER", "client", "other client")
    assert [m["sender"] for m in s.thread(CID)] == ["client", "client", "coach"]
    assert s.unread_for_coach(CID) == 2 and s.unread_for_coach("SYN-OTHER") == 1
    s.mark_read(CID)
    assert s.unread_for_coach(CID) == 0 and s.unread_for_coach("SYN-OTHER") == 1
    assert all(m["read_at"] for m in s.thread(CID) if m["sender"] == "client")
    with pytest.raises(ValueError):
        s.add(CID, "mr_j", "nope")


def test_admin_sees_unread_reply_marks_read(env):
    client, _, db = env
    SupportStore(EventStore(db)).add(CID, "client", "Can we move my check-in day?")
    # GETs show the thread and count but don't mark anything read
    assert client.get(f"/?as_of={AS_OF}").status_code == 200
    r = client.get(f"/client/{CID}?as_of={AS_OF}")
    assert r.status_code == 200
    assert SupportStore(EventStore(db)).unread_for_coach(CID) == 1
    r = client.post(f"/client/{CID}/support/reply", data={"as_of": AS_OF, "message": "  Sure, Friday.  "})
    assert r.status_code == 303 and r.headers["location"].endswith("#support")
    assert "as_of=2026-09-28" in r.headers["location"]
    s = SupportStore(EventStore(db))
    assert [(m["sender"], m["content"]) for m in s.thread(CID)][-1] == ("coach", "Sure, Friday.")
    assert s.unread_for_coach(CID) == 0


def test_admin_reply_validation(env):
    client, _, db = env
    for msg in ("", "   ", "x" * 4001):
        r = client.post(f"/client/{CID}/support/reply", data={"as_of": AS_OF, "message": msg})
        assert r.status_code == 303 and "error=" in r.headers["location"]
    assert SupportStore(EventStore(db)).thread(CID) == []
    assert client.post("/client/NOPE/support/reply", data={"message": "hi"}).status_code == 404


def test_admin_mark_read_route(env):
    client, _, db = env
    SupportStore(EventStore(db)).add(CID, "client", "ping")
    r = client.post(f"/client/{CID}/support/read", data={"as_of": AS_OF})
    assert r.status_code == 303 and r.headers["location"].endswith("#support")
    assert SupportStore(EventStore(db)).unread_for_coach(CID) == 0
    assert client.post("/client/NOPE/support/read").status_code == 404


def test_admin_chat_ignores_client_chat(env):
    client, llm, db = env
    ChatStore(EventStore(db), "client").add(CID, "user", "client-only line")
    client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "coach asks"})
    assert all("client-only line" not in str(m) for m in llm.message_lists[-1])
    client.post(f"/client/{CID}/chat/clear", data={"as_of": AS_OF})
    assert ChatStore(EventStore(db), "coach").history(CID) == []
    assert len(ChatStore(EventStore(db), "client").history(CID)) == 1  # clear only clears its own


def test_cli_chat_uses_coach_audience_checkin_uses_client(monkeypatch, tmp_path, cfg):
    from typer.testing import CliRunner

    import llm.client
    import llm.coach
    import llm.settings
    from ingest.cli import app
    seen = []

    class Session:
        def ask(self, text):
            return type("R", (), {"text": "ok"})()

    def fake_open(*a, **kw):
        seen.append(kw.get("audience"))
        return Session()
    monkeypatch.setattr(llm.coach, "open_session", fake_open)
    monkeypatch.setattr(llm.client, "ClaudeClient", lambda *a, **k: None)
    monkeypatch.setattr(llm.settings, "llm_settings", lambda: {})
    db = tmp_path / "c.db"
    assert CliRunner().invoke(app, ["chat", CID, AS_OF, "--db", str(db)], input="\n").exit_code == 0
    assert CliRunner().invoke(app, ["checkin", CID, AS_OF, "--db", str(db)]).exit_code == 0
    assert seen == ["coach", "client"]


def test_cli_has_client_app_command():
    from typer.testing import CliRunner

    from ingest.cli import app
    r = CliRunner().invoke(app, ["client-app", "--help"])
    assert r.exit_code == 0 and "8766" in r.output and "--no-open" in r.output
