"""Durable agent state in Postgres (schema `agent`) plus short-lived coordination in Redis.

Postgres is the source of truth:
  conversations    one row per chat, owned by exactly one user
  turns            one row per turnId — makes a retried POST idempotent
  events           every streamed event with a per-conversation seq — the transcript and the resume log
  checkpoints      one versioned row per finished turn — what the next turn starts from
  pending_actions  writes the agent proposed and the user has not confirmed yet

Redis holds only what can be lost: the per-conversation lock, the user cache, and the
"chat history is warm" marker. Continuum keeps its own chat history in Redis too.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg
import redis.asyncio as aioredis

from app.settings import settings

pool: asyncpg.Pool
redis: aioredis.Redis

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS agent;
CREATE TABLE IF NOT EXISTS agent.conversations (
  id uuid PRIMARY KEY,
  user_id int NOT NULL,
  title text,
  page_context jsonb,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversations_user_idx ON agent.conversations (user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS agent.turns (
  conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
  turn_id text NOT NULL,
  kind text NOT NULL,
  status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  PRIMARY KEY (conversation_id, turn_id)
);
CREATE TABLE IF NOT EXISTS agent.events (
  conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
  seq bigint NOT NULL,
  turn_id text NOT NULL,
  type text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS agent.checkpoints (
  conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
  version int NOT NULL,
  turn_id text NOT NULL,
  state jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (conversation_id, version)
);
CREATE TABLE IF NOT EXISTS agent.pending_actions (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
  user_id int NOT NULL,
  turn_id text NOT NULL,
  tool_name text NOT NULL,
  arguments jsonb NOT NULL,
  preview jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  decided_at timestamptz
);
"""


async def _ensure_database(url: str) -> None:
    try:
        conn = await asyncpg.connect(url)
        await conn.close()
    except asyncpg.InvalidCatalogNameError:
        parsed = urlparse(url)
        db_name = parsed.path.lstrip("/")
        admin = await asyncpg.connect(urlunparse(parsed._replace(path="/postgres")))
        await admin.execute(f'CREATE DATABASE "{db_name.replace(chr(34), "")}"')
        await admin.close()


async def init() -> None:
    global pool, redis
    await _ensure_database(settings.agent_database_url)
    pool = await asyncpg.create_pool(settings.agent_database_url, init=_init_conn)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await redis.ping()


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def close() -> None:
    await pool.close()
    await redis.aclose()


# ---- conversations ----------------------------------------------------------------------------


async def create_conversation(user_id: int, page_context: dict[str, Any] | None) -> str:
    conv_id = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO agent.conversations (id, user_id, page_context) VALUES ($1, $2, $3)",
        uuid.UUID(conv_id),
        user_id,
        page_context,
    )
    return conv_id


async def get_conversation(conv_id: str, user_id: int) -> asyncpg.Record | None:
    """Ownership check built in: another user's conversation id simply does not exist for you."""
    try:
        cid = uuid.UUID(conv_id)
    except ValueError:
        return None
    return await pool.fetchrow("SELECT * FROM agent.conversations WHERE id = $1 AND user_id = $2", cid, user_id)


async def list_conversations(user_id: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, title, status, updated_at FROM agent.conversations"
        " WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 50",
        user_id,
    )
    return [
        {"id": str(r["id"]), "title": r["title"], "status": r["status"], "updatedAt": r["updated_at"].isoformat()}
        for r in rows
    ]


async def touch_conversation(conv_id: str, title_if_empty: str | None = None) -> None:
    await pool.execute(
        "UPDATE agent.conversations SET updated_at = now(), title = COALESCE(title, $2) WHERE id = $1",
        uuid.UUID(conv_id),
        (title_if_empty or "")[:80] or None,
    )


# ---- turns (idempotency) ----------------------------------------------------------------------


async def begin_turn(conv_id: str, turn_id: str, kind: str) -> str | None:
    """Returns None if this turn is new, else the status of the earlier attempt ('running'/'done'/'failed')."""
    row = await pool.fetchrow(
        """INSERT INTO agent.turns (conversation_id, turn_id, kind, status) VALUES ($1, $2, $3, 'running')
           ON CONFLICT (conversation_id, turn_id) DO NOTHING RETURNING turn_id""",
        uuid.UUID(conv_id),
        turn_id,
        kind,
    )
    if row:
        return None
    return await pool.fetchval(
        "SELECT status FROM agent.turns WHERE conversation_id = $1 AND turn_id = $2", uuid.UUID(conv_id), turn_id
    )


async def finish_turn(conv_id: str, turn_id: str, status: str) -> None:
    await pool.execute(
        "UPDATE agent.turns SET status = $3, finished_at = now() WHERE conversation_id = $1 AND turn_id = $2",
        uuid.UUID(conv_id),
        turn_id,
        status,
    )


# ---- events (transcript + resume log) ------------------------------------------------------------


async def last_seq(conv_id: str) -> int:
    return await pool.fetchval(
        "SELECT COALESCE(MAX(seq), 0) FROM agent.events WHERE conversation_id = $1", uuid.UUID(conv_id)
    )


async def append_event(conv_id: str, seq: int, turn_id: str, event_type: str, payload: dict[str, Any]) -> None:
    await pool.execute(
        "INSERT INTO agent.events (conversation_id, seq, turn_id, type, payload) VALUES ($1, $2, $3, $4, $5)",
        uuid.UUID(conv_id),
        seq,
        turn_id,
        event_type,
        payload,
    )


async def events_after(conv_id: str, after: int, turn_id: str | None = None) -> list[asyncpg.Record]:
    if turn_id:
        return await pool.fetch(
            "SELECT seq, type, payload FROM agent.events WHERE conversation_id = $1 AND turn_id = $2 ORDER BY seq",
            uuid.UUID(conv_id),
            turn_id,
        )
    return await pool.fetch(
        "SELECT seq, type, payload FROM agent.events WHERE conversation_id = $1 AND seq > $2 ORDER BY seq",
        uuid.UUID(conv_id),
        after,
    )


async def recent_transcript(conv_id: str, exchanges: int) -> list[dict[str, str]]:
    """Last N user messages and final assistant replies — used to rebuild context if Redis lost the history."""
    rows = await pool.fetch(
        """SELECT type, payload FROM agent.events
           WHERE conversation_id = $1 AND type IN ('user_message', 'assistant_message')
           ORDER BY seq DESC LIMIT $2""",
        uuid.UUID(conv_id),
        exchanges * 2,
    )
    out = [
        {"role": "user" if r["type"] == "user_message" else "assistant", "text": r["payload"].get("text", "")}
        for r in rows
    ]
    return list(reversed(out))


# ---- checkpoints ------------------------------------------------------------------------------

EMPTY_STATE: dict[str, Any] = {
    "version": 0,
    "turns": 0,
    "entities": {},
    "recentActions": [],
    "pendingActionIds": [],
    "lastSeq": 0,
}


async def latest_checkpoint(conv_id: str) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT version, state FROM agent.checkpoints WHERE conversation_id = $1 ORDER BY version DESC LIMIT 1",
        uuid.UUID(conv_id),
    )
    if not row:
        return dict(EMPTY_STATE)
    return {**row["state"], "version": row["version"]}


async def save_checkpoint(conv_id: str, turn_id: str, state: dict[str, Any]) -> int:
    version = int(state.get("version", 0)) + 1
    await pool.execute(
        "INSERT INTO agent.checkpoints (conversation_id, version, turn_id, state) VALUES ($1, $2, $3, $4)",
        uuid.UUID(conv_id),
        version,
        turn_id,
        {**state, "version": version},
    )
    return version


# ---- pending actions (the write gate) -----------------------------------------------------------


async def create_pending_action(
    conv_id: str, user_id: int, turn_id: str, tool_name: str, arguments: dict[str, Any], preview: dict[str, Any]
) -> dict[str, Any]:
    action_id = uuid.uuid4()
    expires = datetime.now(UTC) + timedelta(hours=settings.pending_action_ttl_hours)
    await pool.execute(
        """INSERT INTO agent.pending_actions
             (id, conversation_id, user_id, turn_id, tool_name, arguments, preview, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        action_id,
        uuid.UUID(conv_id),
        user_id,
        turn_id,
        tool_name,
        arguments,
        preview,
        expires,
    )
    return {
        "actionId": str(action_id),
        "tool": tool_name,
        "arguments": arguments,
        "preview": preview,
        "expiresAt": expires.isoformat(),
    }


async def claim_pending_action(action_id: str, conv_id: str, user_id: int, decision: str) -> asyncpg.Record | None:
    """Atomically moves pending -> executing|rejected. A second click, another user, or an expired action gets None."""
    try:
        aid = uuid.UUID(action_id)
    except ValueError:
        return None
    new_status = "executing" if decision == "approve" else "rejected"
    return await pool.fetchrow(
        """UPDATE agent.pending_actions SET status = $4, decided_at = now()
           WHERE id = $1 AND conversation_id = $2 AND user_id = $3 AND status = 'pending' AND expires_at > now()
           RETURNING *""",
        aid,
        uuid.UUID(conv_id),
        user_id,
        new_status,
    )


async def finish_pending_action(action_id: str, status: str, result: dict[str, Any]) -> None:
    await pool.execute(
        "UPDATE agent.pending_actions SET status = $2, result = $3 WHERE id = $1", uuid.UUID(action_id), status, result
    )


# ---- Redis: lock + warm-history marker ------------------------------------------------------------


async def acquire_lock(conv_id: str, turn_id: str) -> bool:
    ttl = int(settings.turn_timeout_seconds) + 30
    return bool(await redis.set(f"agent:lock:{conv_id}", turn_id, nx=True, ex=ttl))


async def release_lock(conv_id: str, turn_id: str) -> None:
    key = f"agent:lock:{conv_id}"
    if await redis.get(key) == turn_id:
        await redis.delete(key)


async def history_is_warm(conv_id: str) -> bool:
    return bool(await redis.exists(f"agent:warm:{conv_id}"))


async def mark_history_warm(conv_id: str) -> None:
    # Same lifetime as Continuum's session TTL: if this marker is gone, assume its history is gone too.
    await redis.set(f"agent:warm:{conv_id}", "1", ex=172800)
