"""M8 tests. No network: Claude is replaced by a scripted fake with the same
response shape (stop_reason + content blocks)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pytest

from approvals.queue import ApprovalQueue
from ingest.onboarding import load_onboarding_config, onboard
from llm import prompt_builder, retrieval
from llm.client import REFUSAL_TEXT, ClaudeClient
from llm.coach import CHECKIN_REQUEST, open_session
from llm.settings import llm_settings
from llm.tools import TOOLS, ToolError, run_tool
from store.event_store import EventStore
from tests.test_m6_import_onboarding import SAMPLE

CID = "SYN-CUT-01"
AS_OF = date(2026, 9, 28)


def text(t):
    return NS(type="text", text=t)


def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


class FakeAnthropic:
    """Records requests; returns scripted responses in order."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        stop, content = self.script.pop(0)
        return NS(stop_reason=stop, content=content)


@pytest.fixture()
def store(cfg):
    s = EventStore()
    onboard(s, load_onboarding_config(SAMPLE / "onboarding.json"), cfg, base_dir=SAMPLE)
    return s


def _session(store, cfg, script, as_of=AS_OF):
    fake = FakeAnthropic(script)
    return open_session(store, cfg, CID, as_of, ClaudeClient(llm_settings(), client=fake)), fake


# --- retrieval ---------------------------------------------------------------

def test_chunks_follow_chx_headers():
    chunks = retrieval.corpus()
    assert chunks and all(c.ref.startswith("ch") for c in chunks)
    assert {c.source for c in chunks} == {"hypertrophy-principles", "renaissance-diet-2.0"}


def test_retrieval_ranks_relevant_chunk_first():
    [top] = retrieval.retrieve("deload fatigue recovery session", 1)
    assert top.source == "hypertrophy-principles" and "atigue" in top.title
    assert retrieval.retrieve("", 3) == []


# --- prompt builder ------------------------------------------------------------

def test_yaml_is_deterministic_and_quotes_specials():
    y = prompt_builder.to_yaml({"a": {"b": [1, 2]}, "c": "x: y", "d": None, "e": "≈ ok"})
    assert y == 'a:\n  b:\n    - 1\n    - 2\nc: "x: y"\nd: null\ne: ≈ ok'


def test_fill_strips_header_comment_and_rejects_leftovers():
    t = "<!-- {{USER_PROFILE}} doc -->\nHi {{COACH_NAME}} {{USER_PROFILE}} {{SESSION_LOG}} {{KNOWLEDGE}}"
    out = prompt_builder.fill(t, {"COACH_NAME": "Mr. J", "USER_PROFILE": "P", "SESSION_LOG": "S",
                                  "KNOWLEDGE": "K"})
    assert out == "Hi Mr. J P S K"
    with pytest.raises(ValueError):
        prompt_builder.fill("{{OTHER}}", {k: "" for k in ("COACH_NAME", "USER_PROFILE",
                                                          "SESSION_LOG", "KNOWLEDGE")})


def test_system_prompt_has_name_profile_findings_and_no_pending(store, cfg):
    q = ApprovalQueue(store, cfg)
    q.refresh(CID, AS_OF)  # calorie proposal is PENDING
    session, _ = _session(store, cfg, [])
    s = session.system
    assert "You are Mr. J" in s and "{{" not in s and "<!--" not in s
    assert "phase: cut" in s and "week: 7" in s
    assert "rotation candidate" in s and "never collected: soreness ratings" in s
    assert "approved_changes: []" in s
    assert "decrease_calories" not in s  # pending proposals never reach the LLM
    assert "<knowledge>\n[" in s


def test_approved_change_reaches_prompt(store, cfg):
    q = ApprovalQueue(store, cfg)
    q.refresh(CID, AS_OF)
    pid = q.pending(CID)[0].proposal.proposal_id
    q.decide(pid, "approved", "start Monday", at=datetime(2026, 9, 28, 9, tzinfo=timezone.utc))
    session, _ = _session(store, cfg, [])
    assert "change: decrease_calories" in session.system
    assert "coach_note: start Monday" in session.system


def test_session_requires_consent(cfg):
    with pytest.raises(PermissionError):
        open_session(EventStore(), cfg, "nobody", AS_OF, None)


# --- client loop ----------------------------------------------------------------

def test_request_shape(store, cfg):
    session, fake = _session(store, cfg, [("end_turn", [text("Week 7 check-in")])])
    reply = session.ask(CHECKIN_REQUEST)
    assert reply.text == "Week 7 check-in" and not reply.refused
    req = fake.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "medium"}
    assert "thinking" not in req  # can't be disabled on this model; effort controls depth
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["system"] == session.system and req["tools"] == TOOLS
    assert all(t["strict"] for t in req["tools"])


def test_tool_loop_runs_engine_and_returns_final_text(store, cfg):
    script = [
        ("tool_use", [text("Let me check."), tool_use("t1", "calories_for_target_rate",
                                                      {"target_rate_pct_bw": -0.75})]),
        ("end_turn", [text("You'd need about 430 fewer calories a day.")]),
    ]
    session, fake = _session(store, cfg, script)
    reply = session.ask("What would it take to lose 0.75% a week?")
    [call] = reply.tool_calls
    assert (call["name"], call["input"], call["is_error"]) == (
        "calories_for_target_rate", {"target_rate_pct_bw": -0.75}, False)
    assert '"kcal_per_day_change"' in call["result"]
    second = fake.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"  # full content appended, append-only
    [result] = second[-1]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert '"kcal_per_day_change"' in result["content"] and "Not approved" in result["content"]
    # conversation history continues append-only across user turns
    assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "assistant"]


def test_tool_errors_go_back_to_model(store, cfg):
    script = [("tool_use", [tool_use("t1", "no_such_tool", {})]), ("end_turn", [text("ok")])]
    session, fake = _session(store, cfg, script)
    session.ask("hi")
    [result] = fake.calls[1]["messages"][-1]["content"]
    assert result["is_error"] is True and "unknown tool" in result["content"]


def test_refusal_is_not_shown_as_answer(store, cfg):
    session, _ = _session(store, cfg, [("refusal", [text("partial")])])
    reply = session.ask("give me a steroid cycle")
    assert reply.refused and reply.text == REFUSAL_TEXT


def test_runaway_tool_loop_stops(store, cfg):
    loop = [("tool_use", [tool_use(f"t{i}", "search_knowledge", {"query": "x"})]) for i in range(20)]
    session, _ = _session(store, cfg, loop)
    with pytest.raises(RuntimeError):
        session.ask("loop")


# --- tools -------------------------------------------------------------------------

def test_calorie_tool_matches_engine(store, cfg):
    from engine.nutrition.adjustment import kcal_per_day_for_rate_gap
    session, _ = _session(store, cfg, [])
    st = session.state
    import json
    out = json.loads(run_tool("calories_for_target_rate", {"target_rate_pct_bw": -0.75}, st, cfg, 3))
    expected = round(kcal_per_day_for_rate_gap(-0.75 - st.weight.pct_bw_per_week, st.weight.latest_avg_lb, cfg))
    assert out["kcal_per_day_change"] == expected < 0
    assert out["target_within_band"] is True and out["recommended_band_pct_bw"] == [0.5, 1.0]
    crash = json.loads(run_tool("calories_for_target_rate", {"target_rate_pct_bw": -2.0}, st, cfg, 3))
    assert crash["target_within_band"] is False


def test_macro_tool_matches_engine(store, cfg):
    import json
    from engine.nutrition import macros
    session, _ = _session(store, cfg, [])
    st = session.state
    out = json.loads(run_tool("macros_for_calories", {"calories": 2200, "day_type": "moderate"}, st, cfg, 3))
    assert out["macros"] == macros.compute(st.weight.latest_avg_lb, 2200, "cut", "moderate", cfg).macros.model_dump()


def test_calorie_tool_needs_trend(cfg):
    from schemas.state import ClientState
    with pytest.raises(ToolError):
        run_tool("calories_for_target_rate", {"target_rate_pct_bw": -0.5},
                 ClientState(client_id="x", as_of=AS_OF), cfg, 3)


def test_cli_prompt_offline(tmp_path):
    from typer.testing import CliRunner
    from ingest.cli import app
    db = str(tmp_path / "c.db")
    r = CliRunner()
    assert r.invoke(app, ["onboard", str(SAMPLE / "onboarding.json"), "--db", db]).exit_code == 0
    out = r.invoke(app, ["prompt", CID, "2026-09-28", "--db", db])
    assert out.exit_code == 0 and "You are Mr. J" in out.output
