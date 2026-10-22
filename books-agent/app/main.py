"""books-agent HTTP API: app factory and lifespan. Routes live in `app.modules`, wired up in `app.router`."""

from __future__ import annotations

# Continuum reads its settings from the environment at import time — load .env first.
from dotenv import load_dotenv

load_dotenv()

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from continuum.agent import AgentRunner  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.agents.definitions.books_agent import build_agent  # noqa: E402
from app.agents.tools import catalog  # noqa: E402
from app.agents.workflows import live  # noqa: E402
from app.config.llm import configure_llm  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.modules.conversations import store  # noqa: E402
from app.router import api_router  # noqa: E402
from app.utils import backend_client  # noqa: E402


async def keep_tools_fresh(app: FastAPI, model: str) -> None:
    """Re-checks the backend's tool list; when it changed, rebuilds the agent around the new list.

    New turns get the new agent. Turns already running finish on the agent (and tools) they started with.
    """
    while True:
        await asyncio.sleep(settings.tool_refresh_seconds)
        try:
            if not await catalog.refresh():
                continue
            tools = catalog.current()
            app.state.agent = await build_agent(model, settings.max_tool_turns, tools)
            print(f"[agent] tools changed: version {tools.version}, {sorted(tools.specs)}")
        except Exception as exc:  # keep the tools we have; try again next round
            print(f"[agent] tool refresh failed, keeping version {catalog.current().version}: {exc!r}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await store.init()
    backend_client.bind_redis(store.redis)
    model = configure_llm()
    try:
        await catalog.refresh()
    except Exception as exc:
        raise RuntimeError(f"Could not load the agent's tools from {settings.backend_tools_url}: {exc!r}") from exc
    tools = catalog.current()
    app.state.agent = await build_agent(model, settings.max_tool_turns, tools)
    app.state.runner = AgentRunner()
    refresher = asyncio.create_task(keep_tools_fresh(app, model))
    print(
        f"[agent] ready: LLM_BACKEND={settings.llm_backend} model={model} tools={tools.version} {sorted(tools.specs)}"
    )
    yield
    refresher.cancel()
    await live.shutdown()
    await store.close()


def create_app() -> FastAPI:
    app = FastAPI(title="books-agent", lifespan=lifespan)
    app.include_router(api_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.agent_port)
