"""Conversation endpoints. Only books-backend's relay may call them (X-Internal-Auth).

POST /v1/conversations         create a conversation                                  -> JSON
GET  /v1/conversations         the caller's conversations                             -> JSON
POST /v1/conversations/{id}    a turn: {"kind": "message"|"confirm", ...}             -> text/event-stream
GET  /v1/conversations/{id}    resume: events after Last-Event-ID, live until the turn ends -> text/event-stream

The conversation endpoint follows MCP's Streamable HTTP transport: one URL, POST to send and GET to resume, the
client must Accept both application/json and text/event-stream, every event carries an id, and a dropped
connection does not cancel the turn — it keeps running and the client resumes with Last-Event-ID.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.agents.context import TurnContext, User
from app.agents.workflows import live, turns
from app.config.settings import settings
from app.modules.conversations import store
from app.modules.conversations.model import ConfirmIn, NewConversation, Principal, TurnIn
from app.utils import backend_client
from app.utils.backend_client import BackendError

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


# ---- who is calling ------------------------------------------------------------------------------


async def principal(
    authorization: str | None = Header(default=None),
    x_internal_auth: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> Principal:
    # 1. Only the backend relay knows this secret: a browser cannot reach us directly.
    if not x_internal_auth or not hmac.compare_digest(x_internal_auth, settings.internal_auth_secret):
        raise HTTPException(401, "Missing or wrong internal auth")
    if not authorization:
        raise HTTPException(401, "Missing user token")
    # 2. Re-validate the user with the backend itself (cached briefly). Headers are hints, not proof.
    try:
        user: User = await backend_client.validate_user(authorization)
    except BackendError as err:
        raise HTTPException(401, f"Session invalid: {err}") from err
    if x_user_id and x_user_id != str(user.id):
        raise HTTPException(401, "User header does not match the token")
    return Principal(user=user, token=authorization)


# FastAPI-recommended dependency style: the Depends() call lives in the type, not the default value.
CurrentPrincipal = Annotated[Principal, Depends(principal)]


async def owned_conversation(conv_id: str, p: Principal) -> Any:
    conv = await store.get_conversation(conv_id, p.user.id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


def require_accept(accept: str | None, *types: str) -> None:
    if not accept or not all(t in accept for t in types):
        raise HTTPException(406, f"Accept must include {' and '.join(types)}")


# ---- endpoints ------------------------------------------------------------------------------------


SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


def event_stream(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(turns.with_heartbeats(frames), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("")
async def create_conversation(body: NewConversation, p: CurrentPrincipal) -> dict[str, str]:
    conv_id = await store.create_conversation(p.user.id, body.pageContext)
    return {"conversationId": conv_id}


@router.get("")
async def list_conversations(p: CurrentPrincipal) -> list[dict[str, Any]]:
    return await store.list_conversations(p.user.id)


async def _open_turn(
    conv_id: str, turn_id: str, kind: str, p: Principal
) -> TurnContext | StreamingResponse | JSONResponse:
    """Shared turn opening: idempotency, the one-turn-at-a-time lock, and loading the checkpoint."""
    previous = await store.begin_turn(conv_id, turn_id, kind)
    if previous is not None:
        # A retried POST: never run the turn twice. Stream it back instead — the stored events, and, if it is
        # still running, the live ones until it ends.
        return event_stream(turns.follow(conv_id, 0, turn_id=turn_id))
    if not await store.acquire_lock(conv_id, turn_id):
        await store.finish_turn(conv_id, turn_id, "failed")
        return JSONResponse({"error": "Another message is still being answered in this conversation"}, status_code=409)
    warm = await store.history_is_warm(conv_id)
    return TurnContext(
        conversation_id=conv_id,
        turn_id=turn_id,
        user=p.user,
        token=p.token,
        checkpoint=await store.latest_checkpoint(conv_id),
        session_was_warm=warm,
        recent_transcript=[] if warm else await store.recent_transcript(conv_id, settings.history_exchanges_in_prompt),
    )


@router.post("/{conv_id}")
async def post_turn(
    conv_id: str,
    body: TurnIn,
    request: Request,
    p: CurrentPrincipal,
    accept: str | None = Header(default=None),
):
    require_accept(accept, "application/json", "text/event-stream")
    await owned_conversation(conv_id, p)
    ctx = await _open_turn(conv_id, body.turnId, body.kind, p)
    if not isinstance(ctx, TurnContext):
        return ctx
    if isinstance(body, ConfirmIn):
        frames = turns.confirm_turn(ctx, body.actionId, body.decision)
    else:
        frames = turns.message_turn(request.app.state.runner, request.app.state.agent, ctx, body.text)
    # The turn runs on its own; this response is just its first reader. The lock is released when the turn ends.
    live.start(conv_id, body.turnId, frames, on_done=partial(store.release_lock, conv_id, body.turnId))
    return event_stream(turns.follow(conv_id, 0, turn_id=body.turnId))


@router.get("/{conv_id}")
async def resume(
    conv_id: str,
    p: CurrentPrincipal,
    accept: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None),
):
    require_accept(accept, "text/event-stream")
    await owned_conversation(conv_id, p)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return event_stream(turns.follow(conv_id, after))
