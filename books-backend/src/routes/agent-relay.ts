import { Router, type Request, type Response } from 'express';
import { randomUUID } from 'node:crypto';
import { Readable } from 'node:stream';
import { config } from '../config.js';
import { AuthError, validateToken, type SessionUser } from '../auth/session.js';

/**
 * The agent relay — the ONLY way a browser reaches the agent (the agent has no public port in the real design).
 *
 * For every request it:
 *   1. validates the user exactly like a GraphQL request (validateToken)
 *   2. assigns a turnId when the client did not send one (so a retried POST is recognisable)
 *   3. forwards to the agent with the user's raw token + an internal secret only the relay knows
 *   4. pipes the agent's response back byte-for-byte (streams are not buffered)
 *
 * The conversation endpoint is Streamable HTTP (MCP-style): POST /conversations/:id sends a turn and streams it,
 * GET /conversations/:id with Last-Event-ID resumes. Accept and Last-Event-ID are passed through untouched.
 * A browser that goes away only detaches its reader; the agent finishes the turn and the client can resume.
 */
export const agentRelay = Router();

async function authenticate(req: Request, res: Response): Promise<SessionUser | null> {
  try {
    return await validateToken(req.header('authorization'));
  } catch (err) {
    const status = err instanceof AuthError ? err.status : 401;
    res.status(status).json({ error: err instanceof Error ? err.message : 'Unauthorized' });
    return null;
  }
}

function agentHeaders(req: Request, user: SessionUser, turnId?: string): Record<string, string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: req.header('accept') ?? 'application/json',
    authorization: req.header('authorization')!, // the user's own token; the agent uses it for every backend call
    'x-internal-auth': config.internalAuthSecret, // proves the call came through this relay
    'x-user-id': String(user.id), // a hint; the agent re-validates the token itself
    'x-request-id': req.header('x-request-id') ?? randomUUID(),
  };
  if (turnId) headers['x-turn-id'] = turnId;
  const lastEventId = req.header('last-event-id');
  if (lastEventId) headers['last-event-id'] = lastEventId;
  return headers;
}

/** Pipes an upstream fetch Response (JSON or text/event-stream) to the Express response. */
async function pipe(upstream: globalThis.Response, res: Response, req: Request): Promise<void> {
  res.status(upstream.status);
  const contentType = upstream.headers.get('content-type') ?? 'application/json';
  res.setHeader('content-type', contentType);
  const isStream = contentType.includes('text/event-stream');
  if (isStream) {
    res.setHeader('cache-control', 'no-cache');
    res.setHeader('x-accel-buffering', 'no'); // tell nginx-style proxies not to buffer
    res.flushHeaders();
  }
  if (!upstream.body) {
    res.end();
    return;
  }
  const body = Readable.fromWeb(upstream.body as any);
  req.on('close', () => body.destroy()); // browser went away -> stop reading from the agent
  body.pipe(res);
}

async function forward(
  req: Request,
  res: Response,
  method: 'GET' | 'POST',
  agentPath: string,
  body?: unknown,
  turnId?: string,
) {
  const user = await authenticate(req, res);
  if (!user) return;
  try {
    const upstream = await fetch(`${config.agentUrl}${agentPath}`, {
      method,
      headers: agentHeaders(req, user, turnId),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    await pipe(upstream, res, req);
  } catch (err) {
    console.error('[relay] agent unreachable', err);
    if (!res.headersSent) res.status(502).json({ error: 'Agent service unreachable' });
  }
}

// Start a conversation (optionally with page context, like "opened from the editor of site X").
agentRelay.post('/conversations', (req, res) => forward(req, res, 'POST', '/v1/conversations', req.body ?? {}));

// The user's own conversations.
agentRelay.get('/conversations', (req, res) => forward(req, res, 'GET', '/v1/conversations'));

// Send a turn: {kind: 'message', text} or {kind: 'confirm', actionId, decision}. Response is a text/event-stream
// that stays open for the whole turn.
agentRelay.post('/conversations/:id', (req, res) => {
  const turnId = (req.body?.turnId as string | undefined) ?? `t_${randomUUID()}`;
  return forward(
    req,
    res,
    'POST',
    `/v1/conversations/${encodeURIComponent(req.params.id)}`,
    { ...req.body, turnId },
    turnId,
  );
});

// Resume: every stored event after Last-Event-ID, then live ones while a turn is running.
agentRelay.get('/conversations/:id', (req, res) =>
  forward(req, res, 'GET', `/v1/conversations/${encodeURIComponent(req.params.id)}`),
);
