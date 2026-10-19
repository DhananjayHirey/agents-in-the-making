"""Request-scoped state, carried in contextvars.

Why contextvars: Continuum calls our tools and our approval handler from inside its own loop.
Those functions need to know *whose* turn it is (token, user, conversation) without that ever
being a tool argument the model could see or change. asyncio copies contextvars into child
tasks, so values set before runner.run_stream(...) are visible inside every tool call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    role: str


@dataclass
class TurnContext:
    conversation_id: str
    turn_id: str
    user: User
    token: str  # the user's raw JWT — used for backend calls, never shown to the model
    checkpoint: dict[str, Any]  # state loaded at turn start (see store.save_checkpoint)
    session_was_warm: bool  # False when the Redis chat history had expired
    recent_transcript: list[dict[str, str]]  # last N user/assistant pairs from Postgres
    # Filled during the turn:
    pending_created: list[dict[str, Any]] = field(default_factory=list)
    entities_seen: dict[str, str] = field(default_factory=dict)  # book id -> title


current_turn: ContextVar[TurnContext] = ContextVar("current_turn")


def turn() -> TurnContext:
    return current_turn.get()
