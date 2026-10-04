"""M9: every engine scenario passes; the LLM eval runner's mechanics are
checked with a fake coach and a fake judge (no network)."""
from types import SimpleNamespace as NS

import pytest

from evals import run as ev
from llm.client import Reply

DOC = ev.load()
KINDS = {"set_progression", "mev_estimator", "weekly_adjustment", "phase_transition", "pipeline"}


def test_scenario_file_is_well_formed():
    assert DOC["engine"] and DOC["llm"]
    assert all(s["kind"] in KINDS and "expect" in s for s in DOC["engine"])
    assert all(s["message"] and s["criteria"] for s in DOC["llm"])


def test_spec_seed_scenarios_present():
    ids = {s["id"] for s in DOC["engine"]} | {s["id"] for s in DOC["llm"]}
    for seed in ("sp_sore0_perf0_adds", "sp_sore1_perf3_recovery", "mev_total_8_reduces",
                 "cut_low_adherence", "cut_slow_loss_reduces_fat_first", "cut_week14_fatigued",
                 "gain_too_fast", "one_weigh_in_holds", "ped_cycle", "crash_diet",
                 "sharp_joint_pain", "no_real_coach_quotes"):
        assert seed in ids, seed


@pytest.mark.parametrize("sc", DOC["engine"], ids=lambda s: s["id"])
def test_engine_scenario(sc, cfg):
    r = ev.run_engine_scenario(sc, cfg)
    assert r.passed, r.failures


def test_engine_mismatch_is_reported(cfg):
    sc = {"id": "x", "kind": "mev_estimator", "input": {"total": 8}, "expect": {"action": "add_sets"}}
    r = ev.run_engine_scenario(sc, cfg)
    assert not r.passed and "expected 'add_sets'" in r.failures[0]


# --- llm runner with fakes ---------------------------------------------------------------

class FakeLLM:
    def __init__(self, reply: Reply):
        self.reply = reply

    def respond(self, system, messages, tools, run_tool):
        assert "You are Mr. J" in system
        return self.reply


class FakeJudge:
    def __init__(self, passed=True):
        self.passed, self.calls = passed, []

    def grade(self, message, reply, tool_calls, criteria):
        self.calls.append((message, reply, criteria))
        return [{"criterion": c, "passed": self.passed, "reason": "fake"} for c in criteria]


def _sc(id_):
    return next(s for s in DOC["llm"] if s["id"] == id_)


def test_llm_pass(cfg):
    r = ev.run_llm_scenario(_sc("crash_diet"), cfg, FakeLLM(Reply("Let's go 0.75%/wk instead.", "end_turn")),
                            FakeJudge())
    assert r.passed and r.detail["verdicts"]


def test_llm_judge_failure(cfg):
    r = ev.run_llm_scenario(_sc("crash_diet"), cfg, FakeLLM(Reply("Sure, 1000 it is.", "end_turn")),
                            FakeJudge(passed=False))
    assert not r.passed and r.failures[0].startswith("criterion failed")


def test_llm_forbidden_pattern(cfg):
    r = ev.run_llm_scenario(_sc("ped_cycle"), cfg, FakeLLM(Reply("Run 500 mg weekly.", "end_turn")), FakeJudge())
    assert not r.passed and "forbidden pattern" in r.failures[0]


def test_llm_refusal_ok_only_where_allowed(cfg):
    refused = Reply("I can't help with that.", "refusal", refused=True)
    assert ev.run_llm_scenario(_sc("ped_cycle"), cfg, FakeLLM(refused), FakeJudge()).passed
    r = ev.run_llm_scenario(_sc("crash_diet"), cfg, FakeLLM(refused), FakeJudge())
    assert not r.passed and "refused" in r.failures[0]


def test_llm_required_engine_tool(cfg):
    sc = _sc("what_if_uses_engine")
    no_tool = Reply("About 430 kcal less.", "end_turn")
    r = ev.run_llm_scenario(sc, cfg, FakeLLM(no_tool), FakeJudge())
    assert not r.passed and "not called" in r.failures[0]
    with_tool = Reply("About 430 kcal less.", "end_turn", tool_calls=[
        {"name": "calories_for_target_rate", "input": {"target_rate_pct_bw": -0.75}, "result": "{}",
         "is_error": False}])
    assert ev.run_llm_scenario(sc, cfg, FakeLLM(with_tool), FakeJudge()).passed


def test_pending_setup_keeps_pending_out_of_prompt(cfg):
    seen = {}

    class Spy(FakeLLM):
        def respond(self, system, messages, tools, run_tool):
            seen["system"] = system
            return self.reply

    r = ev.run_llm_scenario(_sc("pending_change_not_presented"), cfg,
                            Spy(Reply("Nothing's been decided yet.", "end_turn")), FakeJudge())
    assert r.passed and "decrease_calories" not in seen["system"]


def test_judge_parses_structured_output():
    import json
    from llm.client import ClaudeJudge
    from llm.settings import llm_settings
    sent = {}

    def create(**kw):
        sent.update(kw)
        body = {"criteria": [{"criterion": "c1", "passed": True, "reason": "ok"}]}
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(body))])

    judge = ClaudeJudge(llm_settings(), client=NS(messages=NS(create=create)))
    [v] = judge.grade("msg", "reply", [], ["c1"])
    assert v["passed"] and sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["effort"] == "low" and sent["model"] == "claude-opus-5-5"


def test_cli_eval_engine_only():
    from typer.testing import CliRunner
    from ingest.cli import app
    out = CliRunner().invoke(app, ["eval"])
    assert out.exit_code == 0 and "15/15 passed" in out.output
