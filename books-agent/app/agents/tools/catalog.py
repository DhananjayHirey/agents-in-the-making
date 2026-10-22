"""The agent's tools, as published by books-backend (GET /internal/agent-tools).

The backend owns the tool list: each entry has a name, description, input schema, whether it reads or writes,
and the ONE fixed GraphQL document it runs with the user's token. This module turns each entry into a Continuum
FunctionTool. The model never gets the schema or a "call any query" tool — only these typed functions.

Reads run immediately. Writes are listed in AgentConfig.tool_approval, so Continuum asks our approval handler
first; the handler defers them until the user confirms. When the backend changes a tool, `refresh` notices
(by version) and the agent is rebuilt with the new list.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from continuum.tools.mcp import FunctionTool
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.agents.context import turn
from app.utils import backend_client
from app.utils.backend_client import BackendError, gql

# Typed as MCPServerFunction's `tools` parameter: list is invariant, so list[FunctionTool] would not be accepted.
MCPTool = FunctionTool | Callable[..., Any] | dict[str, Any]

CONFIRM_NOTE = " Requires the user's confirmation before it runs."


class Entity(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    id_field: str
    label_field: str


class ToolSpec(BaseModel):
    """One tool from the backend's manifest (camelCase on the wire)."""

    model_config = ConfigDict(alias_generator=to_camel)

    name: str
    description: str
    input_schema: dict[str, Any]
    kind: Literal["read", "write"]
    document: str
    args_variable: str | None = None
    result_field: str
    entity: Entity | None = None


class ToolCatalog:
    def __init__(self, version: str, specs: list[ToolSpec]):
        self.version = version
        self.specs = {s.name: s for s in specs}
        self.write_tools = {s.name for s in specs if s.kind == "write"}

    def is_write(self, name: str) -> bool:
        return name in self.write_tools

    def function_tools(self) -> list[MCPTool]:
        tools: list[MCPTool] = []
        for spec in self.specs.values():

            async def fn(args: dict[str, Any], _name: str = spec.name) -> dict[str, Any]:
                return await self.run(_name, args)

            description = spec.description + (CONFIRM_NOTE if spec.kind == "write" else "")
            tools.append(FunctionTool(name=spec.name, description=description, input_schema=spec.input_schema, fn=fn))
        return tools

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Runs a tool's GraphQL document as the current turn's user. The confirm endpoint calls this directly."""
        spec = self.specs.get(name)
        if spec is None:
            return {"ok": False, "error": f"The backend no longer offers the tool '{name}'.", "code": "UNKNOWN_TOOL"}
        args = {k: v for k, v in args.items() if v is not None}
        variables = {spec.args_variable: args} if spec.args_variable else args
        try:
            data = await gql(spec.document, variables, turn().token)
        except BackendError as err:
            return {"ok": False, "error": str(err), "code": err.code}
        result = data.get(spec.result_field)
        if spec.entity:
            _remember(result, spec.entity)
        return {"ok": True, "result": result}


def _remember(result: Any, entity: Entity) -> None:
    """Record entities the user has seen this turn, so the checkpoint can resolve "that book" later."""
    seen = turn().entities_seen
    for item in result if isinstance(result, list) else [result]:
        if isinstance(item, dict) and item.get(entity.id_field):
            seen[str(item[entity.id_field])] = str(item.get(entity.label_field, ""))


_current: ToolCatalog | None = None


def current() -> ToolCatalog:
    if _current is None:
        raise RuntimeError("Tool catalog not loaded yet")
    return _current


async def refresh() -> bool:
    """Fetches the manifest; returns True when the tool list changed (always True on the first load)."""
    global _current
    manifest = await backend_client.fetch_tool_manifest(_current.version if _current else None)
    if manifest is None:  # 304: same version as we have
        return False
    _current = ToolCatalog(manifest["version"], [ToolSpec.model_validate(t) for t in manifest["tools"]])
    return True
