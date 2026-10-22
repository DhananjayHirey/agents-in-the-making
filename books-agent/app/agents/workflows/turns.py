"""One chat turn, start to finish, as a stream of Server-Sent Events.

Every event gets the next per-conversation `seq` (the SSE event id) and is written to Postgres BEFORE it is
published, so a client that drops can resume with GET + Last-Event-ID and miss nothing. The turn itself runs
detached from any request (see `live`); `follow` is what a request streams back.

Message turn:  turn_start -> user_message -> [tool_call_start/end, content_delta]* -> assistant_message
               -> [confirm_required]* -> turn_end
Confirm turn:  turn_start -> confirm_received -> tool_call_start/end -> assistant_message -> turn_end
               (no LLM: the stored arguments are executed as-is)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any, cast

from continuum.agent import AgentEvent, AgentRunner, BaseAgent, EventType
from continuum.session import bind_principal

from app.agents.context import TurnContext, current_turn
from app.agents.tools import catalog
from app.agents.workflows import live
from app.config.settings import settings
from app.modules.conversations import store


def sse(seq: int, event_type: str, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


class EventLog:
    """Assigns seq numbers, persists each event, then publishes it to live readers."""

    def __init__(self, conv_id: str, turn_id: str, start_seq: int):
        self.conv_id, self.turn_id, self.seq = conv_id, turn_id, start_seq

    async def emit(self, event_type: str, data: dict[str, Any]) -> str:
        self.seq += 1
        payload = {"turnId": self.turn_id, **data}
        await store.append_event(self.conv_id, self.seq, self.turn_id, event_type, payload)
        frame = sse(self.seq, event_type, payload)
        live.publish(self.conv_id, self.seq, frame)
        return frame


def heartbeat() -> str:
    return ": heartbeat\n\n"  # SSE comment: keeps proxies from closing an idle stream; not stored


async def follow(conv_id: str, after: int, turn_id: str | None = None) -> AsyncIterator[str]:
    """Stored events after `after`, then live ones until the running turn ends. With turn_id, only that turn.

    Subscribing before reading Postgres closes the gap between the two: an event is stored before it is
    published, so anything published before we subscribed is in the rows we read. Duplicates are dropped by seq.
    """
    queue = live.subscribe(conv_id, turn_id)
    try:
        last = after
        for row in await store.events_after(conv_id, after, turn_id=turn_id):
            last = row["seq"]
            yield sse(row["seq"], row["type"], row["payload"])
        if queue is None:
            return  # nothing running: the replay is everything
        while (item := await queue.get()) is not None:
            seq, frame = item
            if seq > last:
                last = seq
                yield frame
    finally:
        if queue is not None:
            live.unsubscribe(conv_id, queue)


async def with_heartbeats(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yields from `source`, inserting a heartbeat whenever it is quiet for heartbeat_seconds."""
    queue: asyncio.Queue[str | None | BaseException] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(item)
            await queue.put(None)
        except BaseException as exc:  # surface errors to the consumer
            await queue.put(exc)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=settings.heartbeat_seconds)
            except TimeoutError:
                yield heartbeat()
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        task.cancel()


def _next_state(ctx: TurnContext, extra_actions: list[dict[str, Any]], last_seq: int) -> dict[str, Any]:
    cp = ctx.checkpoint
    return {
        "version": cp.get("version", 0),
        "turns": int(cp.get("turns", 0)) + 1,
        "entities": {**cp.get("entities", {}), **ctx.entities_seen},
        "recentActions": (cp.get("recentActions", []) + extra_actions)[-10:],
        "pendingActionIds": [a["actionId"] for a in ctx.pending_created],
        "lastSeq": last_seq,
    }


# ---- message turn --------------------------------------------------------------------------------


async def message_turn(runner: AgentRunner, agent: BaseAgent, ctx: TurnContext, text: str) -> AsyncIterator[str]:
    log = EventLog(ctx.conversation_id, ctx.turn_id, await store.last_seq(ctx.conversation_id))
    token = current_turn.set(ctx)
    status = "failed"
    try:
        yield await log.emit("turn_start", {"kind": "message"})
        yield await log.emit("user_message", {"text": text})

        final_text = ""
        streamed = ""

        async def run() -> AsyncIterator[str]:
            nonlocal final_text, streamed
            # bind_principal: Continuum refuses to read/write a session owned by anyone else.
            with bind_principal(str(ctx.user.id)):
                # The runner does not create sessions. get_or_create_session is deterministic for
                # (user, conversation), so every turn of this conversation lands on the same Redis history.
                if runner.session_client is None:
                    raise RuntimeError("Continuum sessions are not configured (is Redis set up?)")
                session_id = await runner.session_client.get_or_create_session(
                    user_id=str(ctx.user.id), conversation_id=ctx.conversation_id
                )
                # run_stream is an async generator but is annotated AsyncIterator; aclosing needs aclose().
                stream = cast(
                    AsyncGenerator[AgentEvent],
                    runner.run_stream(
                        agent,
                        text,
                        session_id=session_id,  # Continuum's Redis chat history
                        user_id=str(ctx.user.id),
                        max_turns=settings.max_tool_turns,
                        metadata={"turnId": ctx.turn_id, "conversationId": ctx.conversation_id},
                    ),
                )
                async with aclosing(stream) as events:
                    async for ev in events:
                        data = ev.data or {}
                        if ev.type == EventType.CONTENT_DELTA and data.get("content"):
                            streamed += data["content"]
                            yield await log.emit("content_delta", {"text": data["content"]})
                        elif ev.type == EventType.TOOL_CALL_START:
                            yield await log.emit("tool_call_start", {"tool": data.get("tool_name")})
                        elif ev.type == EventType.TOOL_CALL_END:
                            yield await log.emit(
                                "tool_call_end",
                                {"tool": data.get("tool_name"), "result": str(data.get("result", ""))[:300]},
                            )
                        elif ev.type == EventType.TOOL_CALL_ERROR:
                            yield await log.emit(
                                "tool_call_error", {"tool": data.get("tool_name"), "error": data.get("error")}
                            )
                        elif ev.type == EventType.RUN_ERROR:
                            yield await log.emit(
                                "error", {"message": data.get("error"), "type": data.get("error_type")}
                            )
                        elif ev.type == EventType.RUN_END:
                            final_text = data.get("content") or ""

        async with asyncio.timeout(settings.turn_timeout_seconds):
            async for frame in run():
                yield frame

        reply = final_text or streamed
        yield await log.emit("assistant_message", {"text": reply})
        await store.mark_history_warm(ctx.conversation_id)

        for action in ctx.pending_created:
            yield await log.emit("confirm_required", action)

        status = "awaiting_confirmation" if ctx.pending_created else "completed"
        state = _next_state(ctx, [], log.seq)
        version = await store.save_checkpoint(ctx.conversation_id, ctx.turn_id, state)
        yield await log.emit("turn_end", {"status": status, "checkpointVersion": version})
    except TimeoutError:
        yield await log.emit("error", {"message": "The turn took too long and was stopped. Nothing was changed."})
        yield await log.emit("turn_end", {"status": "failed"})
    except Exception as exc:  # report, never crash the stream silently
        yield await log.emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        yield await log.emit("turn_end", {"status": "failed"})
    finally:
        current_turn.reset(token)
        await store.finish_turn(ctx.conversation_id, ctx.turn_id, "done" if status != "failed" else "failed")
        await store.touch_conversation(ctx.conversation_id, title_if_empty=text)


# ---- confirm turn ----------------------------------------------------------------------------------


async def confirm_turn(ctx: TurnContext, action_id: str, decision: str) -> AsyncIterator[str]:
    log = EventLog(ctx.conversation_id, ctx.turn_id, await store.last_seq(ctx.conversation_id))
    token = current_turn.set(ctx)
    status = "failed"
    try:
        yield await log.emit("turn_start", {"kind": "confirm"})
        yield await log.emit("confirm_received", {"actionId": action_id, "decision": decision})

        action = await store.claim_pending_action(action_id, ctx.conversation_id, ctx.user.id, decision)
        if action is None:
            yield await log.emit(
                "error", {"message": "That action is no longer pending (already handled, expired, or not yours)."}
            )
            yield await log.emit("turn_end", {"status": "failed"})
            return

        tool, args, summary = (
            action["tool_name"],
            action["arguments"],
            action["preview"].get("summary", action["tool_name"]),
        )
        if decision != "approve":
            record = {"actionId": action_id, "tool": tool, "summary": summary, "outcome": "cancelled"}
            yield await log.emit("assistant_message", {"text": f"Cancelled: {summary}. Nothing was changed."})
            status = "completed"
        else:
            yield await log.emit("tool_call_start", {"tool": tool, "arguments": args})
            # Executed as the CONFIRMING request's user, with their current token: if their access
            # changed since the proposal, the backend's withAuth refuses it here.
            result = await catalog.current().run(tool, args)
            yield await log.emit("tool_call_end", {"tool": tool, "result": result})
            ok = bool(result.get("ok"))
            await store.finish_pending_action(action_id, "executed" if ok else "failed", result)
            record = {"actionId": action_id, "tool": tool, "summary": summary, "outcome": "done" if ok else "refused"}
            if ok:
                text = f"Done: {summary}."
            elif result.get("code") == "FORBIDDEN":
                text = f"The backend refused '{summary}': your role is not allowed to do that."
            else:
                text = f"'{summary}' failed: {result.get('error')}"
            yield await log.emit("assistant_message", {"text": text})
            status = "completed"

        state = _next_state(ctx, [record], log.seq)
        version = await store.save_checkpoint(ctx.conversation_id, ctx.turn_id, state)
        yield await log.emit("turn_end", {"status": status, "checkpointVersion": version})
    except Exception as exc:
        yield await log.emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        yield await log.emit("turn_end", {"status": "failed"})
    finally:
        current_turn.reset(token)
        await store.finish_turn(ctx.conversation_id, ctx.turn_id, "done" if status != "failed" else "failed")
        await store.touch_conversation(ctx.conversation_id)
