"""Talks to books-backend: GraphQL exactly like the frontend does (the user's raw token), plus the internal
tool API (shared secret) that publishes the agent's tools and builds confirm-card previews."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import redis.asyncio as aioredis

from app.agents.context import User
from app.config.settings import settings

_http = httpx.AsyncClient(timeout=20.0)
_redis: aioredis.Redis | None = None


def bind_redis(client: aioredis.Redis) -> None:
    global _redis
    _redis = client


class BackendError(Exception):
    """A GraphQL error from the backend. `code` is FORBIDDEN / UNAUTHENTICATED / ... when known."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


async def gql(query: str, variables: dict[str, Any] | None, token: str) -> dict[str, Any]:
    resp = await _http.post(
        settings.backend_graphql_url,
        json={"query": query, "variables": variables or {}},
        headers={"authorization": token, "content-type": "application/json"},
    )
    body = resp.json()
    if body.get("errors"):
        err = body["errors"][0]
        raise BackendError(err.get("message", "Backend error"), (err.get("extensions") or {}).get("code"))
    return body["data"]


async def fetch_tool_manifest(known_version: str | None) -> dict[str, Any] | None:
    """The backend's tool manifest {version, tools}, or None when it is still `known_version` (304)."""
    headers = {"x-internal-auth": settings.internal_auth_secret}
    if known_version:
        headers["if-none-match"] = f'"{known_version}"'
    resp = await _http.get(settings.backend_tools_url, headers=headers)
    if resp.status_code == 304:
        return None
    resp.raise_for_status()
    return resp.json()


async def preview(tool: str, args: dict[str, Any], token: str) -> dict[str, Any]:
    """The confirm card for a write ({summary, before, after}), built by the backend as the user."""
    resp = await _http.post(
        f"{settings.backend_tools_url}/{tool}/preview",
        json={"args": args},
        headers={"authorization": token, "x-internal-auth": settings.internal_auth_secret},
    )
    resp.raise_for_status()
    return resp.json()


ME = "query Me { me { id email name role } }"


async def validate_user(token: str) -> User:
    """Who is this? Asks the backend (`me`), never trusts headers. Cached briefly per token."""
    key = "agent:user:" + hashlib.sha256(token.encode()).hexdigest()
    if _redis is not None:
        cached = await _redis.get(key)
        if cached:
            return User(**json.loads(cached))
    data = await gql(ME, None, token)
    me = data["me"]
    user = User(id=int(me["id"]), email=me["email"], name=me["name"], role=me["role"])
    if _redis is not None:
        await _redis.set(key, json.dumps(user.__dict__), ex=settings.user_cache_seconds)
    return user
