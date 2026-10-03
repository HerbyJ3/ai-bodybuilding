"""M9 eval runner (BUILD_SPEC §10–11).

python -m evals.run            # engine scenarios (free, deterministic)
python -m evals.run --llm      # + Mr. J guardrail scenarios (Claude API: needs a key, costs money)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from config.loader import Config, load_config
from engine import proposals as proposal_engine
from engine.nutrition import adjustment, phases
from engine.state_builder import build_state
from engine.training import mev_estimator, set_progression
from schemas.events import MacroTargets, make_event
from schemas.state import ClientState, MuscleWeek, PhaseState, WeeklyWeight, WeightTrend

SCENARIOS = Path(__file__).resolve().parent / "scenarios.yaml"
SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "mid_cut_client"
AS_OF = date(2026, 3, 31)


@dataclass
class Result:
    id: str
    group: str
    passed: bool
    desc: str = ""
    failures: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def load(path: Path = SCENARIOS) -> dict[str, list[dict[str, Any]]]:
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for group in ("engine", "llm"):
        ids = [s["id"] for s in doc.get(group, [])]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate {group} scenario ids")
    return doc


# --- engine ----------------------------------------------------------------------------

def _state(i: dict[str, Any]) -> ClientState:
    st = ClientState(client_id="EVAL", as_of=AS_OF, training_age=i.get("training_age"))
    if "phase" in i:
        week = i.get("week", 5)
        st.phase = PhaseState(phase=i["phase"], start_date=AS_OF - timedelta(days=7 * (week - 1)),
                              week=week, target_rate_pct_bw=i.get("target_rate", 0.0),
                              planned_weeks=i.get("planned_weeks", 12), source="coach")
    if "pct_bw_per_week" in i:
        bw, pct, n = i.get("bodyweight_lb", 200.0), i["pct_bw_per_week"], i.get("trend_weeks", 3)
        change = pct / 100 * bw
        st.weight = WeightTrend(
            weekly=[WeeklyWeight(window_end=AS_OF - timedelta(days=7 * k), avg_lb=bw - change * k, n=3)
                    for k in range(n)],
            trend_weeks=n, avg_weekly_change_lb=change, pct_bw_per_week=pct, latest_avg_lb=bw)
    if "adherence" in i or "hunger" in i:
        st.latest_checkin = {"adherence_pct": i.get("adherence", 95), "hunger": i.get("hunger", 3),
                             "energy": 3, "sleep": 3, "notes": "", "date": AS_OF}
    if "macros" in i:
        st.current_macros = {k: MacroTargets(**v) for k, v in i["macros"].items()}
    if "performance" in i and "soreness" not in i:
        st.muscle_weeks = {"quads": [MuscleWeek(meso_id="m", meso_week=3, week_start=AS_OF - timedelta(days=6),
                                                performance=i["performance"])]}
    return st


def _outcome_fields(o) -> dict[str, Any]:
    return {"action": o.kind, "add_min": o.add_min, "add_max": o.add_max}


def _pipeline(i: dict[str, Any], cfg: Config) -> list:
    events = [make_event("EVAL", "phase_started", AS_OF - timedelta(days=70),
                         {"phase": i["phase"], "target_rate_pct_bw": i["target_rate"], "planned_weeks": 12},
                         "coach")]
    for k, n in enumerate(i["weigh_ins_per_week"]):  # most recent week first
        for j in range(n):
            d = AS_OF - timedelta(days=7 * k + 2 * j)
            events.append(make_event("EVAL", "weigh_in", d, {"weight": 200.0, "unit": "lb"}))
    return proposal_engine.generate(build_state(events, AS_OF, cfg), cfg)


def run_engine_scenario(sc: dict[str, Any], cfg: Config) -> Result:
    i, exp, kind = sc["input"], sc["expect"], sc["kind"]
    got: dict[str, Any]
    if kind == "set_progression":
        o = set_progression.decide(i["soreness"], i["performance"], i.get("phase"),
                                   i.get("training_age"), cfg)
        got = _outcome_fields(o)
    elif kind == "mev_estimator":
        got = _outcome_fields(mev_estimator.band_outcome(i["total"], cfg)[0])
    elif kind == "weekly_adjustment":
        [p] = adjustment.evaluate(_state(i), cfg)
        pv = p.proposed_value if isinstance(p.proposed_value, dict) else {}
        got = {"action": p.action, "kcal_per_day_change": pv.get("kcal_per_day_change"),
               "macros": pv.get("macros_by_day_type")}
    elif kind == "phase_transition":
        props = phases.evaluate(_state(i), cfg)
        got = ({"action": props[0].action, "phase": props[0].proposed_value["phase"]}
               if props else {"action": "none"})
    elif kind == "pipeline":
        props = [p for p in _pipeline(i, cfg) if p.rule_id.startswith(exp.get("rule_prefix", ""))]
        got = ({"action": props[0].action, "confidence": props[0].confidence, "count": len(props)}
               if props else {"action": "none"})
        if len(props) != 1:
            return Result(sc["id"], "engine", False, sc.get("desc", ""),
                          [f"expected 1 proposal, got {len(props)}"], {"got": got})
    else:
        raise ValueError(f"unknown engine scenario kind '{kind}'")
    failures = [f"{k}: expected {v!r}, got {got.get(k)!r}"
                for k, v in exp.items() if k != "rule_prefix" and got.get(k) != v]
    return Result(sc["id"], "engine", not failures, sc.get("desc", ""), failures, {"got": got})


# --- llm --------------------------------------------------------------------------------

def _llm_session(sc: dict[str, Any], cfg: Config, llm) -> Any:
    from approvals.queue import ApprovalQueue
    from ingest.onboarding import load_onboarding_config, onboard
    from llm.coach import open_session
    from store.event_store import EventStore
    store = EventStore()
    oc = load_onboarding_config(SAMPLE / "onboarding.json")
    onboard(store, oc, cfg, base_dir=SAMPLE)
    setup = sc.get("setup")
    if setup == "pending_calorie_proposal":
        ApprovalQueue(store, cfg).refresh(oc.client_id, oc.as_of)
    elif setup is not None:
        raise ValueError(f"unknown setup '{setup}'")
    return open_session(store, cfg, oc.client_id, oc.as_of, llm)


def run_llm_scenario(sc: dict[str, Any], cfg: Config, llm, judge) -> Result:
    session = _llm_session(sc, cfg, llm)
    reply = session.ask(sc["message"])
    failures: list[str] = []
    detail: dict[str, Any] = {"reply": reply.text, "refused": reply.refused,
                              "tools": [c["name"] for c in reply.tool_calls]}
    if reply.refused:
        if not sc.get("refusal_ok"):
            failures.append("model refused a request it should have answered")
        return Result(sc["id"], "llm", not failures, sc.get("desc", ""), failures, detail)
    for pat in sc.get("forbid_patterns", []):
        if (m := re.search(pat, reply.text, re.IGNORECASE)):
            failures.append(f"forbidden pattern {pat!r} matched {m.group(0)!r}")
    used = {c["name"] for c in reply.tool_calls if not c.get("is_error")}
    for tool in sc.get("require_tools", []):
        if tool not in used:
            failures.append(f"required engine tool '{tool}' not called")
    verdicts = judge.grade(sc["message"], reply.text, reply.tool_calls, sc.get("criteria", []))
    detail["verdicts"] = verdicts
    failures += [f"criterion failed: {v['criterion']} ({v['reason']})" for v in verdicts if not v["passed"]]
    return Result(sc["id"], "llm", not failures, sc.get("desc", ""), failures, detail)


# --- driver -----------------------------------------------------------------------------

def run(llm_too: bool = False, only: str | None = None, cfg: Config | None = None,
        llm_factory: Callable[[], Any] | None = None, judge_factory: Callable[[], Any] | None = None,
        path: Path = SCENARIOS) -> list[Result]:
    cfg = cfg or load_config()
    doc = load(path)
    results = [run_engine_scenario(sc, cfg) for sc in doc.get("engine", [])
               if only in (None, sc["id"])]
    if llm_too:
        from llm.client import ClaudeClient, ClaudeJudge
        from llm.settings import llm_settings
        make_llm = llm_factory or (lambda: ClaudeClient(llm_settings()))
        judge = (judge_factory or (lambda: ClaudeJudge(llm_settings())))()
        for sc in doc.get("llm", []):
            if only in (None, sc["id"]):
                results.append(run_llm_scenario(sc, cfg, make_llm(), judge))
    return results


def report(results: list[Result]) -> str:
    lines = []
    for r in results:
        lines.append(f"{'PASS' if r.passed else 'FAIL'}  [{r.group}] {r.id}: {r.desc}")
        lines += [f"        - {f}" for f in r.failures]
    n_pass = sum(r.passed for r in results)
    lines.append(f"\n{n_pass}/{len(results)} passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true", help="also run Mr. J scenarios (Claude API, costs money)")
    ap.add_argument("--only", help="run one scenario id")
    ap.add_argument("--json", type=Path, help="write a JSON report here")
    a = ap.parse_args(argv)
    results = run(a.llm, a.only)
    print(report(results))
    if a.json:
        a.json.write_text(json.dumps([asdict(r) for r in results], indent=2, default=str))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
