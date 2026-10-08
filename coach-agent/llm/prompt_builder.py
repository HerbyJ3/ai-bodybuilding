"""Fills prompts/system.md (BUILD_SPEC §8).

{{USER_PROFILE}}  YAML from ClientState
{{SESSION_LOG}}   recent check-ins + APPROVED decisions + history-review findings
{{KNOWLEDGE}}     top-k chunks from retrieval
Pending proposals are never included: the LLM may only present approved changes.
`include_internal=False` (the client app) also drops coach-only text: coach notes on decisions,
notes on macro-target changes, notes the coach typed into a check-in, and history-review
findings (OPEN_ITEMS: the client sees the change and date only).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config.loader import ROOT
from schemas.events import Event
from schemas.state import ClientState
from llm.retrieval import Chunk

TEMPLATE_PATH = ROOT / "prompts" / "system.md"
_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)
_VARS = ("COACH_NAME", "USER_PROFILE", "SESSION_LOG", "KNOWLEDGE")


def to_yaml(value: Any, indent: int = 0) -> str:
    """Minimal deterministic YAML for nested dicts/lists of scalars."""
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:\n{to_yaml(v, indent + 1)}")
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for v in value:
            if isinstance(v, dict) and v:
                inner = to_yaml(v, indent + 1).lstrip()
                lines.append(f"{pad}- {inner}")
            else:
                lines.append(f"{pad}- {_scalar(v)}")
        return "\n".join(lines)
    return pad + _scalar(value)


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, (list, dict)):
        return "[]" if isinstance(v, list) else "{}"
    s = str(v)
    return json.dumps(s, ensure_ascii=False) if (s == "" or any(c in s for c in ":#{}[],&*!|>'\"%@`") or s != s.strip()) else s


def user_profile(state: ClientState, include_internal: bool = True) -> dict[str, Any]:
    w = state.weight
    prof: dict[str, Any] = {
        "client_id": state.client_id,
        "as_of": state.as_of.isoformat(),
        "training_age": state.training_age,
        "phase": None,
        "bodyweight": {
            "latest_weekly_avg_lb": round(w.latest_avg_lb, 1) if w.latest_avg_lb else None,
            "pct_bw_per_week": round(w.pct_bw_per_week, 2) if w.pct_bw_per_week is not None else None,
            "weeks_of_trend_data": w.trend_weeks,
        },
        "maintenance_kcal": ({"kcal": state.maintenance.kcal, "confidence": state.maintenance.confidence}
                             if state.maintenance else None),
        "avg_intake_kcal_last_14d": state.avg_intake_kcal,
        "current_macros": ({k: v.model_dump() for k, v in state.current_macros.items()}
                           if state.current_macros else None),
        "mesocycle": None,
    }
    if state.phase:
        prof["phase"] = {"phase": state.phase.phase, "week": state.phase.week,
                         "target_rate_pct_bw": state.phase.target_rate_pct_bw}
        if state.phase.planned_weeks is not None:
            prof["phase"]["planned_weeks"] = state.phase.planned_weeks
    if state.meso:
        m = state.meso
        prof["mesocycle"] = {"week": m.week, "weeks_planned": m.weeks_planned,
                             "rir_target": list(m.rir_target) if m.rir_target else "deload",
                             "exercises": m.exercises_per_muscle}
    if state.limitations:
        prof["limitations"] = [{"area": lim.area, "restrictions": lim.restrictions,
                                **({"notes": lim.description} if lim.description else {})}
                               for lim in state.limitations]
    if state.target_changes:
        prof["macro_target_history"] = [
            {"date": c.date.isoformat(), "day_type": c.day_type,
             "macros": c.after.model_dump(), **({"note": c.note} if c.note and include_internal else {})}
            for c in state.target_changes[-6:]]
    if state.joint_pain:
        prof["joint_pain_last_7d"] = state.joint_pain
    return prof


def logging_gaps(events: list[Event], as_of: date, days: int) -> dict[str, Any]:
    """Which of the last `days` days (ending `as_of`) have no weigh-in, daily target or check-in,
    so Mr. J can tell the client what is missing and that he is estimating from what was logged."""
    window = [as_of - timedelta(days=i) for i in range(days - 1, -1, -1)]
    logged: dict[str, set[date]] = {"weigh_in": set(), "daily_target": set(), "check_in": set()}
    for e in events:
        if e.type == "weigh_in":
            logged["weigh_in"].add(e.day)
        elif e.type == "macro_adherence_logged":
            logged["daily_target"].add(e.payload.date)
        elif e.type == "weekly_checkin":
            logged["check_in"].add(e.day)
    missing = {kind: [d.isoformat() for d in window if d not in got] for kind, got in logged.items()}
    return {
        "window": f"{window[0].isoformat()} to {as_of.isoformat()} ({days} days, today included)",
        "today": as_of.isoformat(),
        "missing_days": missing,
        "days_with_nothing_logged": [d.isoformat() for d in window
                                     if all(d not in got for got in logged.values())],
    }


def session_log(state: ClientState, approved: list[Event], findings: list[dict[str, str]],
                max_decisions: int = 5, include_internal: bool = True,
                gaps: dict[str, Any] | None = None) -> dict[str, Any]:
    decisions = []
    for e in approved[:max_decisions]:
        p = e.payload.proposal or {}
        d = {"decided_on": e.day.isoformat(), "decision": e.payload.decision,
             "change": p.get("action"), "target": p.get("target"),
             "value": e.payload.final_value, "engine_rationale": p.get("rationale_short")}
        if include_internal:
            d["coach_note"] = e.payload.coach_note or None
        decisions.append(d)
    checkins = [{"date": c["date"].isoformat() if isinstance(c["date"], date) else c["date"],
                 **{k: c.get(k) for k in ("adherence_pct", "hunger", "energy", "sleep", "sleep_hours",
                                          "training_feel", "notes") if c.get(k) not in (None, "")
                    # notes the coach typed into a check-in are internal; the client's own notes are not
                    and (k != "notes" or include_internal or c.get("source") == "client")}}
                for c in state.checkins_recent[:2]]
    log = {
        "note": "Only coach-APPROVED changes are listed. Present nothing else as a plan change.",
        "approved_changes": decisions,
        "recent_checkins": checkins,
        "checkin_scales": "hunger/energy 1=low, 3=mid, 5=high; sleep_hours = hours slept",
        "daily_macro_adherence": state.macro_adherence,
    }
    if gaps is not None:
        log["logging_this_week"] = gaps
    if include_internal:
        log["history_review_findings"] = [f"[{f['area']}] {f['finding']}" for f in findings]
    return log


def fill(template: str, values: dict[str, str]) -> str:
    out = _COMMENT.sub("", template, count=1)
    for k in _VARS:
        out = out.replace("{{" + k + "}}", values[k])
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if leftover:
        raise ValueError(f"unfilled template variables: {leftover}")
    return out


def build_system_prompt(coach_name: str, state: ClientState, approved: list[Event],
                        findings: list[dict[str, str]], knowledge: list[Chunk],
                        template_path: Path = TEMPLATE_PATH, include_internal: bool = True,
                        gaps: dict[str, Any] | None = None) -> str:
    return fill(template_path.read_text(encoding="utf-8"), {
        "COACH_NAME": coach_name,
        "USER_PROFILE": to_yaml(user_profile(state, include_internal)),
        "SESSION_LOG": to_yaml(session_log(state, approved, findings, include_internal=include_internal,
                                           gaps=gaps)),
        "KNOWLEDGE": "\n\n".join(c.render() for c in knowledge) or "(no matching passages)",
    })
