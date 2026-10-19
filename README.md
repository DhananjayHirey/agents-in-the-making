# Agent POC — books-backend ⇄ books-agent (Continuum + Claude on Bedrock)

A miniature of the CMS-agent design, small enough to read in one sitting:

- **books-backend** (Node, TypeScript): stands in for the CMS backend.
  - Users live in Postgres and books in Mongo.
  - A GraphQL API offers `getBook`, `listBooks`, `setBook` and `deleteBook`, guarded by `withAuth` and roles.
  - It has a login route, an **agent relay**, and a demo chat page.
- **books-agent** (Python 3.13, FastAPI, Continuum): stands in for `atlas-agent`.
  - It receives chat turns from the relay only, and runs a Continuum agent whose tools call the backend's GraphQL as the signed-in user.
  - Writes wait for the user's confirm.
  - It keeps the transcript, checkpoints and pending actions in Postgres.
  - It keeps short-term chat history in Continuum's Redis session.

```
browser / CLI ──POST /api/agent/conversations/:id/messages──▶ books-backend relay
                                                              │ validateToken (same as GraphQL)
                                                              │ + turnId, X-Internal-Auth, user's token
                                                              ▼
                                                      books-agent  /v1/conversations/:id/messages
                                                              │ check secret, validate user via `me`
                                                              │ idempotency (turnId), lock, load checkpoint
                                                              │ Continuum AgentRunner.run_stream
                                                              │   ├─ Claude (Bedrock)  ◀─▶ tool calls
                                                              │   └─ tools ──POST /graphql (user's token)──▶ books-backend
                                                              │ writes → approval_handler → pending action (deferred)
                                                              ▼
                     text/event-stream (seq-numbered events, piped back through the relay)
browser ──POST …/confirm {actionId}──▶ relay ──▶ agent executes the stored mutation (no LLM) ──▶ backend
```

## Run it

Prerequisites: Postgres, Mongo and Redis running locally (6379); Node 20+; Python 3.13; AWS credentials with
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
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m app.main      # http://localhost:8001 — creates agent_poc DB + agent schema
```

**3. Chat**

- **Browser:** open http://localhost:4000, pick a user and sign in (passwords are in `books-backend/src/db/seed-users.ts`).
- **Terminal:**

  ```bash
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
| User validation (agent): secret, then backend `me`, cached | `books-agent/app/main.py` → `principal`, `app/backend_client.py` → `validate_user` |
| turnId idempotency + one turn at a time | `app/store.py` → `begin_turn`, `acquire_lock`; `app/main.py` → `_start_turn` |
| Streamable HTTP events with seq + resume | `app/turns.py` → `EventLog`, `GET /events?after=` |
| Chat history (short-term) | Continuum session (Redis) via `run_stream(session_id=conversation_id)` + `bind_principal` |
| Checkpoints + transcript (durable) | `app/store.py` (`agent.checkpoints`, `agent.events`); injected each model call by `agent_def.inject_turn_context` |
| Tools calling the backend as the user | `app/tools.py` (fixed GraphQL docs, token from contextvar) |
| Write gate | `AgentConfig.tool_approval` + `app/approval.py` (deferred) → `turns.confirm_turn` |
| Bedrock / gateway switch | `app/llm.py` (`LLM_BACKEND`) |

## Gotchas found while building

- **anthropic SDK 1.x vs Continuum 2.1.** The SDK dropped `temperature` and `top_p` as typed arguments, but Continuum's `AnthropicProvider` still passes them, which raises `TypeError`. `BedrockClaudeProvider._build_kwargs` moves them into `extra_body`. The same bug would hit Continuum's built-in direct-Anthropic path; worth reporting to the Continuum team.
- **Continuum's session Redis defaults to port 6380.** `.env` pins `SESSION_REDIS_PORT=6379`.
- **Continuum refuses a Redis without a password** and reports the session store as "degraded" (a fail-closed `SessionOwnershipError`). For local work only, `.env` sets `CONTINUUM_ALLOW_INSECURE=1`. Anywhere shared, set `SESSION_REDIS_PASSWORD` instead.
- **MCP tools need an explicit `ToolExecutor`.** `mcp_servers=[...]` alone raises `AgentConfigurationError`. `build_agent` creates the executor, initialises it, and passes `tool_executor=` and `tools=executor.get_tool_definitions()`.
- **The runner does not create chat sessions.** Without `session_client.get_or_create_session(user_id, conversation_id)` first, history is silently not saved. `turns.message_turn` does this each turn.
- **Agent-level memory is on by default** even with `MEMORY_ENABLED=false`. It is turned off with `AgentMemoryConfig(search_memories=False, store_memories=False)`.
- **Startup warns about `books__delete_book` running without a `policy_store`.** In this POC the backend's `withAuth` is the real gate and every write goes through the confirm step. Wiring a Continuum `PolicyStore` is the next hardening step.
- **No `[default]` AWS profile.** Set `AWS_PROFILE` to a named profile whose credentials are current. Temporary session tokens expire.
