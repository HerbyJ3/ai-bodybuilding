"""Mr. J: assembles context from the engine and talks through an LLMClient."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from approvals.queue import ApprovalQueue
from config.loader import Config
from engine import history_review
from engine.state_builder import build_state
from llm import prompt_builder, retrieval
from llm.client import LLMClient, Reply
from llm.settings import llm_settings
from llm.tools import TOOLS, run_tool
from store.event_store import EventStore

CHECKIN_REQUEST = ("Write this week's check-in message to the client. Cover what is going well, "
                   "the approved changes and why, and what to focus on next. Use only numbers "
                   "from the profile, session log or tool results.")


@dataclass
class CoachSession:
    llm: LLMClient
    system: str
    state: Any
    cfg: Config
    top_k: int
    messages: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, text: str) -> Reply:
        self.messages.append({"role": "user", "content": text})
        return self.llm.respond(self.system, self.messages, TOOLS,
                                lambda name, args: run_tool(name, args, self.state, self.cfg, self.top_k))


COACH_AUDIENCE = """

## Who you are talking to in this session
The person messaging you is this client's **coach**, not the client. Talk about the client in the
third person, help the coach understand the data and decide, and draft client-facing messages when
asked (written to the client, in your voice). The coach approves changes in the dashboard; you never
approve anything, and you still present only approved changes as decided."""


def open_session(store: EventStore, cfg: Config, client_id: str, as_of: date, llm: LLMClient,
                 settings: dict[str, Any] | None = None, audience: str = "client",
                 history: list[dict[str, str]] | None = None) -> CoachSession:
    """`history`: earlier turns as [{"role": "user"|"assistant", "content": text}], so a saved
    conversation can continue. `audience="coach"` frames Mr. J as talking to the coach."""
    s = settings or llm_settings()
    if not store.has_consent(client_id):
        raise PermissionError(f"no consent recorded for {client_id}")
    events = store.read(client_id)
    state = build_state(events, as_of, cfg)
    review = history_review.review(events, state, cfg)
    approved = [e for e in ApprovalQueue(store, cfg).approved(client_id) if e.day <= as_of]
    query = " ".join(f["finding"] for f in review["findings"]) or "weekly check-in"
    knowledge = retrieval.retrieve(query, s["knowledge_top_k"])
    system = prompt_builder.build_system_prompt(s["coach_name"], state, approved,
                                                review["findings"], knowledge)
    if audience == "coach":
        system += COACH_AUDIENCE
    elif audience != "client":
        raise ValueError(f"unknown audience {audience!r}")
    messages = [{"role": m["role"], "content": m["content"]} for m in (history or [])]
    return CoachSession(llm, system, state, cfg, s["knowledge_top_k"], messages)
