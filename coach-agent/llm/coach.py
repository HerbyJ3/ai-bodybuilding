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

    def ask(self, text: str, attachments: list[dict[str, Any]] | None = None) -> Reply:
        """`attachments`: content blocks from llm.attachments.to_block (documents go before the text)."""
        content: Any = text if not attachments else [*attachments, {"type": "text", "text": text}]
        self.messages.append({"role": "user", "content": content})
        return self.llm.respond(self.system, self.messages, TOOLS,
                                lambda name, args: run_tool(name, args, self.state, self.cfg, self.top_k))


COACH_AUDIENCE = """

## Who you are talking to in this session
The person messaging you is this client's **coach**, not the client. Talk about the client in the
third person, help the coach understand the data and decide, and draft client-facing messages when
asked (written to the client, in your voice). The coach approves changes in the dashboard; you never
approve anything, and you still present only approved changes as decided."""

_CLIENT_FRAMING = """

## Who you are talking to in this session
The person messaging you is the **client**. Speak to them directly ("you", "your plan").
- Present only the approved changes in `<session_log>` as decided. Never mention pending items, a
  review queue, proposals, or changes that might be coming.
- If they ask you to change their plan, explain that their coach reviews and decides plan changes.
  You can explain the reasoning and answer what-if questions, but nothing changes until the coach
  decides."""

# "client": the client, outside the app (CLI check-in drafts, evals). The coach reviews these.
CLIENT_GENERIC_AUDIENCE = _CLIENT_FRAMING + """
- For support issues (billing, scheduling, account questions, wanting to talk to their coach, or
  reporting pain or an injury), tell them to contact their coach directly. You cannot pass messages
  along, so never say you will forward, share or tell the coach anything. Your safety guidance on
  pain and injury still applies."""

# "client_app": the client in the client app, which has a "Message your coach" button.
CLIENT_AUDIENCE = _CLIENT_FRAMING + """
- For support issues (billing, scheduling, problems with the app, account questions, wanting to talk
  to their coach, or reporting pain or an injury), tell them to use **"Message your coach"** in the
  app. You cannot pass messages along, so never say you will forward, share or tell the coach
  anything. Your safety guidance on pain and injury still applies."""

# audience -> (closing block, include coach-only text: coach notes, target-change notes, findings)
AUDIENCES: dict[str, tuple[str, bool]] = {
    "coach": (COACH_AUDIENCE, True),
    "client": (CLIENT_GENERIC_AUDIENCE, True),
    "client_app": (CLIENT_AUDIENCE, False),
}


def open_session(store: EventStore, cfg: Config, client_id: str, as_of: date, llm: LLMClient,
                 settings: dict[str, Any] | None = None, audience: str = "client",
                 history: list[dict[str, str]] | None = None) -> CoachSession:
    """`history`: earlier turns as [{"role": "user"|"assistant", "content": text}], so a saved
    conversation can continue. `audience` frames who Mr. J is talking to: "coach" (dashboard, CLI
    chat), "client" (default; CLI check-in drafts and evals, which the coach reviews) or
    "client_app" (the client app: points support to "Message your coach" and leaves out coach-only
    notes and history-review findings)."""
    if audience not in AUDIENCES:
        raise ValueError(f"unknown audience {audience!r}")
    closing, include_internal = AUDIENCES[audience]
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
                                                review["findings"], knowledge,
                                                include_internal=include_internal) + closing
    messages = [{"role": m["role"], "content": m["content"]} for m in (history or [])]
    return CoachSession(llm, system, state, cfg, s["knowledge_top_k"], messages)
