"""LLM client (BUILD_SPEC §8). The single place that talks to the provider
(Claude); everything else uses the `LLMClient` protocol, so it can be swapped
or faked in tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

ToolRunner = Callable[[str, dict[str, Any]], str]


@dataclass
class Reply:
    text: str
    stop_reason: str
    refused: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_content: list[Any] = field(default_factory=list)  # full blocks of the final turn


class LLMClient(Protocol):
    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                run_tool: ToolRunner) -> Reply: ...


REFUSAL_TEXT = ("I can't help with that one. If it's about your health or medication, "
                "please talk to your physician.")


class ClaudeClient:
    """Claude via the Anthropic SDK with a manual tool loop.

    - Thinking can't be disabled on the default model; depth is set with effort.
    - `fallbacks: "default"` re-runs a safety-declined request on Anthropic's
      recommended fallback model server-side; a refusal that still comes back
      is handled, never shown as an answer.
    - `messages` is appended to in place (full content blocks, including
      thinking), so a chat history stays append-only.
    """

    def __init__(self, settings: dict[str, Any], client: Any = None) -> None:
        import anthropic  # imported lazily so the engine works without the SDK installed
        self.s = settings
        self.client = client or anthropic.Anthropic()

    def _create(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        return self.client.beta.messages.create(
            model=self.s["model"],
            max_tokens=self.s["max_tokens"],
            output_config={"effort": self.s["effort"]},
            system=system,
            messages=messages,
            tools=tools,
            cache_control={"type": "ephemeral"},
            betas=[self.s["refusal_fallback_beta"]],
            fallbacks=self.s["refusal_fallbacks"],
        )

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                run_tool: ToolRunner) -> Reply:
        calls: list[dict[str, Any]] = []
        for _ in range(self.s["max_tool_rounds"] + 1):
            resp = self._create(system, messages, tools)
            if resp.stop_reason == "refusal":
                return Reply(REFUSAL_TEXT, "refusal", refused=True, tool_calls=calls)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text")
                return Reply(text, resp.stop_reason, tool_calls=calls, assistant_content=resp.content)
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                calls.append({"name": b.name, "input": b.input})
                try:
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": run_tool(b.name, dict(b.input))})
                except Exception as exc:  # tool errors go back to the model, not the user
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": f"Error: {exc}", "is_error": True})
            messages.append({"role": "user", "content": results})
        raise RuntimeError("tool loop did not finish within max_tool_rounds")
