"""books-agent HTTP API. Only books-backend's relay may call it (X-Internal-Auth).

POST /v1/conversations                      create a conversation
GET  /v1/conversations                      the caller's conversations
POST /v1/conversations/{id}/messages        send a message      -> text/event-stream
POST /v1/conversations/{id}/confirm         confirm/reject a write -> text/event-stream
GET  /v1/conversations/{id}/events?after=N  replay stored events after seq N -> text/event-stream
"""

from __future__ import annotations

# Continuum reads its settings from the environment at import time — load .env first.
from dotenv import load_dotenv

load_dotenv()

import hmac  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import Annotated, Any  # noqa: E402

from continuum.agent import AgentRunner  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app import backend_client, store, turns  # noqa: E402
from app.agent_def import build_agent  # noqa: E402
from app.backend_client import BackendError  # noqa: E402
from app.context import TurnContext, User  # noqa: E402
from app.llm import configure_llm  # noqa: E402
from app.settings import settings  # noqa: E402

state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await store.init()
    backend_client.bind_redis(store.redis)
    model = configure_llm()
    state["agent"] = await build_agent(model, settings.max_tool_turns)
    state["runner"] = AgentRunner()
    print(f"[agent] ready: LLM_BACKEND={settings.llm_backend} model={model}")
    yield
    await store.close()


app = FastAPI(title="books-agent", lifespan=lifespan)


# ---- who is calling ------------------------------------------------------------------------------


class Principal(BaseModel):
    user: Any
    token: str


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


# ---- endpoints ------------------------------------------------------------------------------------


class NewConversation(BaseModel):
    pageContext: dict[str, Any] | None = None


class MessageIn(BaseModel):
    turnId: str = Field(min_length=3, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    turnId: str = Field(min_length=3, max_length=100)
    actionId: str
    decision: str = Field(pattern="^(approve|reject)$")


SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/conversations")
async def create_conversation(body: NewConversation, p: CurrentPrincipal) -> dict[str, str]:
    conv_id = await store.create_conversation(p.user.id, body.pageContext)
    return {"conversationId": conv_id}


@app.get("/v1/conversations")
async def list_conversations(p: CurrentPrincipal) -> list[dict[str, Any]]:
    return await store.list_conversations(p.user.id)


async def _start_turn(
    conv_id: str, turn_id: str, kind: str, p: Principal
) -> TurnContext | StreamingResponse | JSONResponse:
    """Shared turn opening: idempotency, the one-turn-at-a-time lock, and loading the checkpoint."""
    previous = await store.begin_turn(conv_id, turn_id, kind)
    if previous == "running":
        return JSONResponse({"error": "This turn is still running"}, status_code=409)
    if previous in ("done", "failed"):
        return StreamingResponse(
            turns.replay_turn(conv_id, turn_id), media_type="text/event-stream", headers=SSE_HEADERS
        )
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


async def _with_lock_release(gen: AsyncIterator[str], conv_id: str, turn_id: str) -> AsyncIterator[str]:
    try:
        async for frame in gen:
            yield frame
    finally:
        await store.release_lock(conv_id, turn_id)


@app.post("/v1/conversations/{conv_id}/messages")
async def send_message(conv_id: str, body: MessageIn, p: CurrentPrincipal):
    await owned_conversation(conv_id, p)
    ctx = await _start_turn(conv_id, body.turnId, "message", p)
    if not isinstance(ctx, TurnContext):
        return ctx
    stream = turns.message_turn(state["runner"], state["agent"], ctx, body.text)
    return StreamingResponse(
        _with_lock_release(stream, conv_id, body.turnId), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.post("/v1/conversations/{conv_id}/confirm")
async def confirm(conv_id: str, body: ConfirmIn, p: CurrentPrincipal):
    await owned_conversation(conv_id, p)
    ctx = await _start_turn(conv_id, body.turnId, "confirm", p)
    if not isinstance(ctx, TurnContext):
        return ctx
    stream = turns.confirm_turn(ctx, body.actionId, body.decision)
    return StreamingResponse(
        _with_lock_release(stream, conv_id, body.turnId), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.get("/v1/conversations/{conv_id}/events")
async def events(conv_id: str, request: Request, p: CurrentPrincipal, after: int = 0):
    await owned_conversation(conv_id, p)
    last_event_id = request.headers.get("last-event-id")
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

    async def gen() -> AsyncIterator[str]:
        for row in await store.events_after(conv_id, start):
            yield turns.sse(row["seq"], row["type"], row["payload"])

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.agent_port)
