"""The Continuum agent: model, system prompt, tools, approval gate, and per-turn context injection."""

from __future__ import annotations

import json

from continuum.agent import AgentConfig, BaseAgent
from continuum.agent.config import AgentMemoryConfig
from continuum.tools import ToolExecutor
from continuum.tools.mcp import MCPServerFunction

from app.agents.context import current_turn
from app.agents.middleware.approval import make_approval_handler
from app.agents.prompts.books import SYSTEM_PROMPT
from app.agents.tools.catalog import ToolCatalog


def inject_turn_context(instructions: str, _run_ctx) -> str:
    """Runs before every model call (Continuum instruction_modifiers).

    Adds what we know from the durable checkpoint: who the user is, which books are already resolved,
    what was recently done. If Continuum's Redis history expired, we also replay the last few exchanges
    from Postgres so the conversation continues seamlessly.
    """
    try:
        ctx = current_turn.get()
    except LookupError:
        return instructions
    cp = ctx.checkpoint
    lines = [
        "",
        "## Conversation state (from the last checkpoint)",
        f"User: {ctx.user.name} (first name only for tone; access is decided by the backend).",
        f"Known books (id -> title): {json.dumps(cp.get('entities', {}))}",
        f"Recent actions: {json.dumps(cp.get('recentActions', [])[-5:])}",
    ]
    if not ctx.session_was_warm and ctx.recent_transcript:
        lines.append("Chat history was restored from storage. Earlier exchanges:")
        for msg in ctx.recent_transcript:
            lines.append(f"- {msg['role']}: {msg['text'][:400]}")
    return instructions + "\n".join(lines)


async def build_agent(model: str, max_tool_turns: int, tools: ToolCatalog) -> BaseAgent:
    """Builds the agent around the backend's current tool list. Rebuilt whenever that list changes."""
    # Our tools run in-process as an MCP "function server". Continuum 2.1 executes MCP tools only
    # through an explicit ToolExecutor; the model sees them namespaced as "books__<tool>".
    books_server = MCPServerFunction("books", tools.function_tools())
    executor = ToolExecutor(tool_registry={books_server: None})
    await executor.initialize()
    return BaseAgent(
        name="books-agent",
        instructions=SYSTEM_PROMPT,
        model=model,
        temperature=0.2,
        max_tokens=1024,
        tool_executor=executor,
        tools=executor.get_tool_definitions(),
        instruction_modifiers=[inject_turn_context],
        # Long-term memory (mem0 + vector store) is out of scope: durable state is our Postgres checkpoint.
        memory_config=AgentMemoryConfig(search_memories=False, store_memories=False),
        config=AgentConfig(
            max_turns=max_tool_turns,
            # The backend marks which tools write. fnmatch patterns: the namespaced 'books__set_book' and a bare
            # 'set_book' alike (but not 'reset_book').
            tool_approval={p for name in tools.write_tools for p in (name, f"*__{name}")},
            approval_handler=make_approval_handler(tools),
        ),
    )
