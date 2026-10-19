"""The write gate, plugged into Continuum's tool-approval hook.

Continuum calls approval_handler BEFORE any tool listed in AgentConfig.tool_approval runs, and passes
the exact arguments. We never approve inline (that would hold the HTTP request open while a person
decides). Instead we store the call as a pending action and answer `deferred`: Continuum tells the model
the call "is awaiting approval", the model says so to the user, and the turn ends. The confirm endpoint
later executes exactly the stored arguments — the model cannot change them in between.
"""

from __future__ import annotations

from typing import Any

from continuum.agent.approval import ToolApprovalDecision, ToolApprovalRequest

from app import store
from app.backend_client import BackendError, gql
from app.context import turn
from app.tools import BOOK_FIELDS, WRITE_TOOLS


def base_tool_name(name: str) -> str:
    """MCP tools reach the model namespaced as '<server>__<tool>'; we store the plain name."""
    return name.split("__")[-1]


async def _current_book(book_id: str | None) -> dict[str, Any] | None:
    if not book_id:
        return None
    try:
        data = await gql(
            f"query GetBook($id: ID!) {{ getBook(id: $id) {{ {BOOK_FIELDS} }} }}", {"id": book_id}, turn().token
        )
        return data["getBook"]
    except BackendError:
        return None


async def build_preview(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Before/after the user sees on the confirm card."""
    if tool == "set_book":
        before = await _current_book(args.get("id"))
        after = {k: v for k, v in args.items() if v is not None}
        return {
            "summary": ("Update" if before else "Create") + f" book '{args.get('title')}'",
            "before": before,
            "after": after,
        }
    if tool == "delete_book":
        before = await _current_book(args.get("id"))
        return {
            "summary": f"Delete book '{(before or {}).get('title', args.get('id'))}'",
            "before": before,
            "after": None,
        }
    return {"summary": tool, "before": None, "after": args}


async def approval_handler(req: ToolApprovalRequest) -> ToolApprovalDecision:
    tool = base_tool_name(req.tool_name)
    if tool not in WRITE_TOOLS:  # defensive: only writes are routed here
        return ToolApprovalDecision(approved=True, reviewer="policy", reason="read-only tool")

    ctx = turn()
    preview = await build_preview(tool, dict(req.arguments))
    action = await store.create_pending_action(
        ctx.conversation_id, ctx.user.id, ctx.turn_id, tool, dict(req.arguments), preview
    )
    ctx.pending_created.append(action)
    return ToolApprovalDecision(
        approved=False,
        deferred=True,
        reason=f"Waiting for {ctx.user.name} to confirm: {preview['summary']}. Nothing has been changed yet.",
    )
