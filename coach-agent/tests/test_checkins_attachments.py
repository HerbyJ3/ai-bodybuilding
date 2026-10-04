"""Dashboard check-in dropdowns, daily 'Macros hit?' logs, and chat file attachments.
Synthetic data only; Mr. J is a fake (no network)."""
import base64
import io
import zipfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dashboard.app import create_app
from dashboard.store import ChatStore
from engine.nutrition import adjustment
from engine.state_builder import build_state
from ingest.onboarding import load_onboarding_config, onboard
from llm.attachments import AttachmentError, docx_text, to_block
from llm.client import Reply
from store.event_store import EventStore
from tests.conftest import CID as TCID, ev, state
from tests.test_m6_import_onboarding import SAMPLE

CID, AS_OF = "SYN-CUT-01", "2026-09-28"


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


# --- schema & engine -----------------------------------------------------------------

def test_macro_adherence_validation():
    ok = ev("macro_adherence_logged", date(2026, 9, 1), {"date": "2026-09-01", "hit": False,
                                                         "off_by": {"carb_g": 30}})
    assert ok.payload.off_by == {"carb_g": 30}
    with pytest.raises(ValidationError):
        ev("macro_adherence_logged", date(2026, 9, 1), {"date": "2026-09-01", "hit": False})
    with pytest.raises(ValidationError):
        ev("macro_adherence_logged", date(2026, 9, 1), {"date": "2026-09-01", "hit": True,
                                                         "off_by": {"fat_g": 10}})
    with pytest.raises(ValidationError):
        ev("macro_adherence_logged", date(2026, 9, 1), {"date": "2026-09-01", "hit": False,
                                                         "off_by": {"fiber_g": 10}})


def test_checkin_optional_fields():
    e = ev("weekly_checkin", date(2026, 9, 1), {"hunger": 5, "energy": 1, "sleep_hours": 6,
                                                "training_feel": "crap"})
    assert e.payload.adherence_pct is None and e.payload.sleep is None


def _days(as_of, pattern):
    return [ev("macro_adherence_logged", as_of - timedelta(days=i),
               {"date": as_of - timedelta(days=i), "hit": hit, **({} if hit else {"off_by": {"carb_g": 40}})})
            for i, hit in enumerate(pattern)]


def test_daily_logs_summarised_latest_wins(cfg):
    as_of = date(2026, 3, 31)
    events = _days(as_of, [True, True, False, True])
    events.append(ev("macro_adherence_logged", as_of - timedelta(days=2),
                     {"date": as_of - timedelta(days=2), "hit": True}, "coach"))  # corrected later
    events.append(ev("macro_adherence_logged", as_of - timedelta(days=10),
                     {"date": as_of - timedelta(days=10), "hit": False, "off_by": {"fat_g": 10}}))  # outside window
    ma = build_state(events, as_of, cfg).macro_adherence
    assert (ma["days_logged"], ma["days_hit"], ma["pct"]) == (4, 4, 100)


def test_daily_logs_stand_in_for_adherence(cfg):
    st = state("cut", pct=-0.2, adherence=None)
    st.macro_adherence = {"days_logged": 5, "days_hit": 2, "pct": 40, "window_days": 7, "recent": []}
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "adherence_intervention" and "daily macro logs" in p.rationale_short
    st.macro_adherence = {"days_logged": 6, "days_hit": 6, "pct": 100, "window_days": 7, "recent": []}
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "decrease_calories" and "no_recent_checkin" not in p.data_quality_flags
    assert p.inputs_used["adherence_source"].startswith("daily macro logs")


def test_too_few_daily_logs_ignored(cfg):
    st = state("cut", pct=-0.2, adherence=None)
    st.macro_adherence = {"days_logged": 2, "days_hit": 0, "pct": 0, "window_days": 7, "recent": []}
    [p] = adjustment.evaluate(st, cfg)
    assert p.action == "decrease_calories" and "no_recent_checkin" in p.data_quality_flags


# --- attachments ------------------------------------------------------------------------

def test_pdf_becomes_document_block():
    b = to_block("plan.pdf", b"%PDF-1.4 fake")
    assert b["type"] == "document" and b["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(b["source"]["data"]) == b"%PDF-1.4 fake"


def test_docx_and_txt_become_text():
    assert docx_text(_docx(["Push day", "Smith press 3x10"])) == "Push day\nSmith press 3x10"
    b = to_block("results.docx", _docx(["Week 3 results"]))
    assert b["type"] == "text" and "Week 3 results" in b["text"] and 'name="results.docx"' in b["text"]
    assert "squat 3x8" in to_block("log.txt", b"squat 3x8")["text"]


@pytest.mark.parametrize("name,data,msg", [
    ("old.doc", b"x", "save it as .docx"), ("x.exe", b"x", "only PDF"),
    ("empty.txt", b"", "empty"), ("bad.docx", b"not a zip", "readable"),
    ("big.pdf", b"x" * (10 * 1024 * 1024 + 1), "larger than"),
])
def test_attachment_errors(name, data, msg):
    with pytest.raises(AttachmentError, match=msg):
        to_block(name, data)


# --- dashboard --------------------------------------------------------------------------

class FakeLLM:
    def __init__(self):
        self.calls = []

    def respond(self, system, messages, tools, run_tool):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return Reply("Got it, reviewing your file.", "end_turn")


@pytest.fixture()
def env(tmp_path, cfg):
    data = tmp_path / "data"
    (data / "syn").mkdir(parents=True)
    onboard(EventStore(data / "syn" / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    llm = FakeLLM()
    return TestClient(create_app(data, cfg, llm_factory=lambda: llm)), llm, data / "syn" / "coach.db"


def test_checkin_form_saves_mapped_values(env, cfg):
    client, llm, db = env
    r = client.post(f"/client/{CID}/checkin-log", data={"as_of": AS_OF, "hunger": "high",
                                                        "energy": "low", "training_feel": "fantastic",
                                                        "sleep_hours": "6"}, follow_redirects=True)
    assert "Check-in saved" in r.text and "fantastic" in r.text and "6 h" in r.text
    c = [e for e in EventStore(db).read(CID) if e.type == "weekly_checkin"][-1]
    assert (c.day, c.payload.hunger, c.payload.energy, c.payload.sleep_hours, c.payload.training_feel,
            c.source.value) == (date(2026, 9, 28), 5, 1, 6.0, "fantastic", "coach")  # the page's As-of date
    r = client.post(f"/client/{CID}/checkin-log", data={"as_of": AS_OF, "hunger": "starving", "energy": "mid"},
                    follow_redirects=True)
    assert "must be low, mid or high" in r.text


def test_macros_hit_form(env):
    client, _, db = env
    r = client.post(f"/client/{CID}/macros-hit", data={"as_of": AS_OF, "when": "2026-09-27", "hit": "yes"},
                    follow_redirects=True)
    assert "Daily target logged" in r.text
    r = client.post(f"/client/{CID}/macros-hit", data={"as_of": AS_OF, "when": "2026-09-26", "hit": "no",
                                                       "off_carbs": "40", "off_fat": "-10", "off_protein": ""},
                    follow_redirects=True)
    assert "1/2" in r.text and "carbs +40 g" in r.text and "fat -10 g" in r.text
    logs = [e.payload for e in EventStore(db).read(CID) if e.type == "macro_adherence_logged"]
    assert logs[0].off_by == {"carb_g": 40.0, "fat_g": -10.0}
    r = client.post(f"/client/{CID}/macros-hit", data={"as_of": AS_OF, "hit": "no"}, follow_redirects=True)
    assert "not saved" in r.text


def test_mr_j_sees_checkin_and_macro_logs(env):
    client, llm, _ = env
    client.post(f"/client/{CID}/checkin-log", data={"as_of": AS_OF, "hunger": "mid", "energy": "high",
                                                    "training_feel": "good", "sleep_hours": "7"})
    client.post(f"/client/{CID}/macros-hit", data={"as_of": AS_OF, "hit": "yes"})
    client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "How's the week?"})
    system = llm.calls[-1]["system"]
    assert "sleep_hours: 7" in system and "training_feel: good" in system
    assert "daily_macro_adherence:" in system and "days_hit: 1" in system


def test_chat_with_pdf_attachment(env):
    client, llm, db = env
    r = client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "My new plan"},
                    files=[("files", ("plan.pdf", b"%PDF-1.4 plan", "application/pdf"))], follow_redirects=True)
    assert "Got it, reviewing your file." in r.text and "[Attached: plan.pdf]" in r.text
    content = llm.calls[-1]["messages"][-1]["content"]
    assert content[0]["type"] == "document" and content[-1] == {"type": "text", "text": "My new plan"}
    saved = list((db.parent / "attachments").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("-plan.pdf")
    # later turns keep only the text note, not the file
    client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "And next?"})
    history = llm.calls[-1]["messages"]
    assert history[0]["content"] == "My new plan\n\n[Attached: plan.pdf]"


def test_chat_attachment_only_and_errors(env):
    client, llm, db = env
    r = client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": ""},
                    files=[("files", ("results.txt", b"bench 3x8 @ smith 135"))], follow_redirects=True)
    assert "Please review it" in llm.calls[-1]["messages"][-1]["content"][-1]["text"]
    n = len(llm.calls)
    r = client.post(f"/client/{CID}/chat", data={"as_of": AS_OF, "message": "old file"},
                    files=[("files", ("plan.doc", b"x"))], follow_redirects=True)
    assert "save it as .docx" in r.text and len(llm.calls) == n
    assert ChatStore(EventStore(db)).history(CID)[-1]["content"] != "old file"


def test_checkin_resave_replaces_and_prefills(env, cfg):
    client, _, db = env
    client.post(f"/client/{CID}/checkin-log", data={"as_of": AS_OF, "hunger": "low", "energy": "low",
                                                    "notes": "rough week"})
    r = client.get(f"/client/{CID}?as_of={AS_OF}")
    form = r.text.split('id="checkins"')[1].split("</form>")[0]
    assert 'value="rough week"' in form and '<option value="low" selected>' in form
    assert 'name="when"' not in form  # date comes from the page header
    assert form.index('name="notes"') < form.index('name="hunger"') < form.index("Save check-in")
    assert form.count('class="clear"') == 5  # notes + 4 dropdowns
    client.post(f"/client/{CID}/checkin-log", data={"as_of": AS_OF, "hunger": "mid", "energy": "high"})
    st = build_state(EventStore(db).read(CID), date(2026, 9, 28), cfg)
    todays = [c for c in st.checkins_recent if c["date"] == date(2026, 9, 28)]
    assert len(todays) == 1 and (todays[0]["hunger"], todays[0]["notes"]) == (3, "")


def test_daily_target_non_training_checkbox(env, cfg):
    client, _, db = env
    r = client.get(f"/client/{CID}?as_of={AS_OF}")
    assert "Daily Target" in r.text and "Macros hit?" not in r.text
    assert "Low-carb (non-training) target: 190 P / 150 C / 70 F" in r.text
    assert "Moderate target: 190 P / 230 C / 65 F" in r.text
    client.post(f"/client/{CID}/macros-hit", data={"as_of": AS_OF, "when": "2026-09-27", "hit": "yes",
                                                   "non_training": "1"})
    client.post(f"/client/{CID}/macros-hit", data={"as_of": AS_OF, "when": "2026-09-26", "hit": "yes"})
    logs = {e.payload.date: e.payload.day_type for e in EventStore(db).read(CID)
            if e.type == "macro_adherence_logged"}
    assert logs == {date(2026, 9, 27): "non_training", date(2026, 9, 26): "moderate"}
    r = client.get(f"/client/{CID}?as_of={AS_OF}")
    assert "2026-09-27 (non-training): hit" in r.text
