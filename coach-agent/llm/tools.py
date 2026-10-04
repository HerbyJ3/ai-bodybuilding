"""Tools Mr. J can call. Every number comes from the rules engine (BUILD_SPEC §8:
'the LLM never computes numbers'). Results are hypothetical, never plan changes."""
from __future__ import annotations

import json
from typing import Any

from config.loader import Config
from engine.nutrition import adjustment, macros
from llm.retrieval import retrieve
from schemas.state import ClientState

NOT_A_PLAN = "Hypothetical engine result. Not approved: any plan change needs coach approval."

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge",
        "description": "Search the coaching knowledge base (training and nutrition principles) "
                       "for passages relevant to a question. Use before explaining a principle "
                       "that is not already in <knowledge>.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "calories_for_target_rate",
        "description": "Engine calculation: the daily calorie change needed to move this client's "
                       "current bodyweight trend to a target weekly rate (% bodyweight per week; "
                       "negative = losing). Use for any 'what if I lost/gained X per week' question. "
                       "Never do this math yourself.",
        "strict": True,
        "input_schema": {"type": "object",
                         "properties": {"target_rate_pct_bw": {"type": "number"}},
                         "required": ["target_rate_pct_bw"], "additionalProperties": False},
    },
    {
        "name": "macros_for_calories",
        "description": "Engine calculation: protein, carbs and fat for a daily calorie total and "
                       "day type, for this client's bodyweight and phase. Never do this math yourself.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {
            "calories": {"type": "number"},
            "day_type": {"type": "string", "enum": ["non_training", "light", "moderate", "hard"]}},
            "required": ["calories", "day_type"], "additionalProperties": False},
    },
]


class ToolError(ValueError):
    pass


def run_tool(name: str, args: dict[str, Any], state: ClientState, cfg: Config, top_k: int) -> str:
    if name == "search_knowledge":
        chunks = retrieve(str(args["query"]), top_k)
        return "\n\n".join(c.render() for c in chunks) or "No matching passages."
    bw = state.weight.latest_avg_lb
    if name == "calories_for_target_rate":
        if bw is None or state.weight.pct_bw_per_week is None:
            raise ToolError("not enough weigh-in data for a bodyweight trend yet")
        target = float(args["target_rate_pct_bw"])
        gap = target - state.weight.pct_bw_per_week
        out: dict[str, Any] = {
            "current_rate_pct_bw_per_week": round(state.weight.pct_bw_per_week, 2),
            "target_rate_pct_bw_per_week": target,
            "kcal_per_day_change": round(adjustment.kcal_per_day_for_rate_gap(gap, bw, cfg)),
        }
        phase = state.phase.phase if state.phase else None
        if phase in ("cut", "gain"):
            lo, hi = cfg.nutrition(f"phases.{phase}.rate_pct_bw_per_week")
            directional = -target if phase == "cut" else target
            out["recommended_band_pct_bw"] = [lo, hi]
            out["target_within_band"] = lo <= directional <= hi
        return json.dumps({**out, "note": NOT_A_PLAN})
    if name == "macros_for_calories":
        if bw is None:
            raise ToolError("no bodyweight on record")
        r = macros.compute(bw, float(args["calories"]), state.phase.phase if state.phase else None,
                           str(args["day_type"]), cfg)
        return json.dumps({"macros": r.macros.model_dump(), "flags": r.flags, "note": NOT_A_PLAN})
    raise ToolError(f"unknown tool {name}")
