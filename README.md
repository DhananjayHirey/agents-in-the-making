# Agent POC — books-frontend → books-backend ⇄ books-agent (Continuum + Claude on Bedrock)

A miniature of the CMS-agent design, small enough to read in one sitting:

- **books-backend** (Node, TypeScript): stands in for the CMS backend.
  - Users live in Postgres and books in Mongo.
  - A GraphQL API offers `getBook`, `listBooks`, `setBook` and `deleteBook`, guarded by `withAuth` and roles.
  - It has a login route and an **agent relay**.
- **books-frontend** (Vite, React, TypeScript): the demo chat UI. It talks only to the backend (`/auth`, `/api/agent`).
- **books-agent** (Python 3.13, FastAPI, Continuum): stands in for `atlas-agent`.
  - It receives chat turns from the relay only, and runs a Continuum agent whose tools call the backend's GraphQL as the signed-in user.
  - Writes wait for the user's confirm.
  - It keeps the transcript, checkpoints and pending actions in Postgres.
  - It keeps short-term chat history in Continuum's Redis session.

```
browser / CLI ──POST /api/agent/conversations/:id {kind:'message'}──▶ books-backend relay
                                                              │ validateToken (same as GraphQL)
                                                              │ + turnId, X-Internal-Auth, user's token
                                                              ▼
                                                      books-agent  POST /v1/conversations/:id
                                                              │ check secret, validate user via `me`
                                                              │ idempotency (turnId), lock, load checkpoint
                                                              │ turn runs as a background task (survives disconnects)
                                                              │ Continuum AgentRunner.run_stream
                                                              │   ├─ Claude (Bedrock)  ◀─▶ tool calls
                                                              │   └─ tools ──POST /graphql (user's token)──▶ books-backend
                                                              │ writes → approval_handler → pending action (deferred)
                                                              │ tool list ◀──GET /internal/agent-tools (polled, ETag)── books-backend
                                                              ▼
                     text/event-stream (events with id = seq, piped back through the relay)
browser ──POST …/:id {kind:'confirm', actionId}──▶ relay ──▶ agent executes the stored mutation (no LLM) ──▶ backend
browser ──GET  …/:id  Last-Event-ID: N──▶ relay ──▶ agent replays events after N, then follows the running turn live
```

The conversation endpoint uses MCP-style **Streamable HTTP**: one URL per conversation, `POST` sends a turn and
streams it back, `GET` with `Last-Event-ID` resumes. Clients send `Accept: application/json, text/event-stream`.
A dropped connection does not cancel the turn; the web page and the CLI resume automatically.

**The backend owns the agent's tools.** `books-backend/src/agent/tools.ts` lists every tool: name, description,
input schema, `read` or `write`, and the one fixed GraphQL document it runs with the user's token. Write tools
also build their own confirm card (`preview`). The agent loads the list from `GET /internal/agent-tools` at
startup and re-checks it every `TOOL_REFRESH_SECONDS` (30 by default; `If-None-Match`, so unchanged lists cost a
304). When the list changes, the agent is rebuilt with it; turns already running finish on the old tools. To add
or change a tool, edit `tools.ts` and restart the backend — the agent needs no change.

## Run it

Prerequisites: Postgres, Mongo and Redis running locally (6379); Node 20+; [uv](https://docs.astral.sh/uv/) (installs Python 3.13 for you); AWS credentials with
Bedrock access to the Claude model in `BEDROCK_MODEL_ID`.

**1. Backend**

```bash
cd books-backend
cp .env.example .env        # fill DATABASE_URL, JWT_SECRET, INTERNAL_AUTH_SECRET
npm install
npm start                   # http://localhost:4000 — creates books_poc DB, seeds users + books
```

**2. Agent**

```bash
cd books-agent
cp .env.example .env        # fill AGENT_DATABASE_URL, the SAME INTERNAL_AUTH_SECRET, AWS_REGION, AWS_PROFILE
uv sync                     # creates .venv with Python 3.13 and installs deps from uv.lock
uv run python -m app.main   # http://localhost:8001 — creates agent_poc DB + agent schema
```

**3. Frontend**

```bash
cd books-frontend
npm install
npm run dev                 # http://localhost:5173 — proxies /auth and /api to the backend on :4000
```

**4. Chat**

- **Browser:** open http://localhost:5173, pick a user and sign in (passwords are in `books-backend/src/db/seed-users.ts`).
- **Terminal:**

  ```bash
  cd books-backend
  npm run chat -- --user editor@books.local --password <from seed-users.ts>
  ```

Things to try:

| Say | What happens |
|---|---|
| "Which sci-fi books do we have?" | `books__list_books` runs and the answer streams back |
| "Change Dune's year to 1966" (as editor) | `list_books`/`get_book` run, then `set_book` is deferred and a confirm card shows before/after. Confirm runs `setBook` |
| "Delete Clean Code" (as editor) | A confirm card appears. On Confirm the backend refuses (admin only) and the agent says so |
| "Delete Clean Code" (as admin) | Confirm deletes it |
| Refresh, then "Replay from storage" | All events come back from Postgres by seq |
| Flush Redis, then keep chatting | History is rebuilt from the Postgres transcript and checkpoint |

## Where each concept lives

| Concept | Code |
|---|---|
| User validation (backend) | `books-backend/src/auth/session.ts` → `validateToken`, used by GraphQL `withAuth` and by the relay |
| Relay: auth, turnId, internal secret, unbuffered pipe | `books-backend/src/routes/agent-relay.ts` |
| User validation (agent): secret, then backend `me`, cached | `books-agent/app/modules/conversations/routes.py` → `principal`, `app/utils/backend_client.py` → `validate_user` |
| turnId idempotency + one turn at a time | `app/modules/conversations/store.py` → `begin_turn`, `acquire_lock`; `app/modules/conversations/routes.py` → `_start_turn` |
| Streamable HTTP: one endpoint, events with seq ids, resume via `Last-Event-ID` | `app/modules/conversations/routes.py` → `post_turn`, `resume`; `app/agents/workflows/turns.py` → `EventLog`, `follow` |
| Turns survive client disconnects | `app/agents/workflows/live.py` (background task + in-process fan-out; Redis pub/sub with several replicas) |
| Chat history (short-term) | Continuum session (Redis) via `run_stream(session_id=conversation_id)` + `bind_principal` |
| Checkpoints + transcript (durable) | `app/modules/conversations/store.py` (`agent.checkpoints`, `agent.events`); injected each model call by `agents/definitions/books_agent.inject_turn_context` |
| Tool list owned by the backend | `books-backend/src/agent/tools.ts` → `AGENT_TOOLS`; served by `src/routes/agent-tools.ts` |
| Tools calling the backend as the user | `app/agents/tools/catalog.py` (backend's fixed GraphQL docs, token from contextvar); refreshed by `app/main.py` → `keep_tools_fresh` |
| Write gate | `AgentConfig.tool_approval` (the backend's `write` tools) + `app/agents/middleware/approval.py` (deferred; preview from the backend) → `turns.confirm_turn` |
| Bedrock / gateway switch | `app/config/llm.py` (`LLM_BACKEND`) |

## Gotchas found while building

- **anthropic SDK 1.x vs Continuum 2.1.** The SDK dropped `temperature` and `top_p` as typed arguments, but Continuum's `AnthropicProvider` still passes them, which raises `TypeError`. `BedrockClaudeProvider._build_kwargs` moves them into `extra_body`. The same bug would hit Continuum's built-in direct-Anthropic path; worth reporting to the Continuum team.
- **Continuum's session Redis defaults to port 6380.** `.env` pins `SESSION_REDIS_PORT=6379`.
- **Continuum refuses a Redis without a password** and reports the session store as "degraded" (a fail-closed `SessionOwnershipError`). For local work only, `.env` sets `CONTINUUM_ALLOW_INSECURE=1`. Anywhere shared, set `SESSION_REDIS_PASSWORD` instead.
- **MCP tools need an explicit `ToolExecutor`.** `mcp_servers=[...]` alone raises `AgentConfigurationError`. `build_agent` creates the executor, initialises it, and passes `tool_executor=` and `tools=executor.get_tool_definitions()`.
- **The runner does not create chat sessions.** Without `session_client.get_or_create_session(user_id, conversation_id)` first, history is silently not saved. `turns.message_turn` does this each turn.
- **Agent-level memory is on by default** even with `MEMORY_ENABLED=false`. It is turned off with `AgentMemoryConfig(search_memories=False, store_memories=False)`.
- **Startup warns about `books__delete_book` running without a `policy_store`.** In this POC the backend's `withAuth` is the real gate and every write goes through the confirm step. Wiring a Continuum `PolicyStore` is the next hardening step.
- **No `[default]` AWS profile.** Set `AWS_PROFILE` to a named profile whose credentials are current. Temporary session tokens expire.
