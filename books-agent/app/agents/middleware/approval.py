"""The write gate, plugged into Continuum's tool-approval hook.

Continuum calls approval_handler BEFORE any tool listed in AgentConfig.tool_approval runs, and passes
the exact arguments. We never approve inline (that would hold the HTTP request open while a person
decides). Instead we store the call as a pending action and answer `deferred`: Continuum tells the model
the call "is awaiting approval", the model says so to the user, and the turn ends. The confirm endpoint
later executes exactly the stored arguments — the model cannot change them in between.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from continuum.agent.approval import ToolApprovalDecision, ToolApprovalRequest

from app.agents.context import turn
from app.agents.tools.catalog import ToolCatalog
from app.modules.conversations import store
from app.utils import backend_client


def base_tool_name(name: str) -> str:
    """MCP tools reach the model namespaced as '<server>__<tool>'; we store the plain name."""
    return name.split("__")[-1]


async def build_preview(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Before/after the user sees on the confirm card. The backend builds it, since it owns the data."""
    try:
        return await backend_client.preview(tool, args, turn().token)
    except httpx.HTTPError:
        return {"summary": tool, "before": None, "after": args}


def make_approval_handler(tools: ToolCatalog) -> Callable[[ToolApprovalRequest], Awaitable[ToolApprovalDecision]]:
    """The handler is bound to the tool list its agent was built with, so a refresh can never turn a write
    the agent still offers into an unconfirmed one."""

    async def approval_handler(req: ToolApprovalRequest) -> ToolApprovalDecision:
        tool = base_tool_name(req.tool_name)
        if tool not in tools.specs:
            return ToolApprovalDecision(approved=False, reviewer="policy", reason=f"Unknown tool '{tool}'.")
        if not tools.is_write(tool):  # defensive: only writes are routed here
            return ToolApprovalDecision(approved=True, reviewer="policy", reason="read-only tool")
        return await _defer(tool, dict(req.arguments))

    return approval_handler


async def _defer(tool: str, args: dict[str, Any]) -> ToolApprovalDecision:
    ctx = turn()
    preview = await build_preview(tool, args)
    action = await store.create_pending_action(ctx.conversation_id, ctx.user.id, ctx.turn_id, tool, args, preview)
    ctx.pending_created.append(action)
    return ToolApprovalDecision(
        approved=False,
        deferred=True,
        reason=f"Waiting for {ctx.user.name} to confirm: {preview['summary']}. Nothing has been changed yet.",
    )
