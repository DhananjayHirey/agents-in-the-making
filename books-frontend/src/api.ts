/**
 * Calls to books-backend. The agent is reached only through the backend's relay at /api/agent.
 *
 * The conversation endpoint is Streamable HTTP: POST /api/agent/conversations/:id sends a turn and streams its
 * events back as text/event-stream; GET on the same URL with Last-Event-ID resumes. The stream is read with fetch,
 * because EventSource can neither POST nor send the authorization header.
 */
import type { AgentEvent, TurnBody, User } from './types';

export type OnEvent = (event: AgentEvent) => void;

/** The last event id seen, shared across the POST and any resume GETs of the same conversation. */
export interface Cursor {
  lastSeq: number;
}

export class HttpError extends Error {}

function request(token: string | null, path: string, init: RequestInit & { headers?: Record<string, string> } = {}) {
  return fetch(path, {
    ...init,
    headers: {
      'content-type': 'application/json',
      // Streamable HTTP: the client accepts both; the server picks JSON or an event stream.
      accept: 'application/json, text/event-stream',
      ...(token ? { authorization: token } : {}),
      ...init.headers,
    },
  });
}

async function json<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => ({})); // e.g. the dev proxy's empty 500 when the backend is down
  if (!res.ok) throw new HttpError(body.error ?? `HTTP ${res.status} (is books-backend running on :4000?)`);
  return body as T;
}

export async function login(email: string, password: string): Promise<{ token: string; user: User }> {
  return json(await request(null, '/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }));
}

export async function createConversation(token: string): Promise<string> {
  const body = await json<{ conversationId: string }>(
    await request(token, '/api/agent/conversations', { method: 'POST', body: '{}' }),
  );
  return body.conversationId;
}

/**
 * Reads a text/event-stream response, calling onEvent for each event.
 * Returns true when the stream ended with turn_end, false when it stopped early (dropped or cut off).
 * A non-stream response (an HTTP error) throws HttpError: that is an answer, not something to resume.
 */
async function readStream(res: Response, cursor: Cursor, onEvent: OnEvent): Promise<boolean> {
  if (!res.ok || !(res.headers.get('content-type') ?? '').includes('text/event-stream')) {
    throw new HttpError(`HTTP ${res.status}: ${await res.text()}`);
  }
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let ended = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return ended;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = parseFrame(frame);
        if (!event) continue;
        if (event.seq) cursor.lastSeq = Math.max(cursor.lastSeq, event.seq);
        if (event.type === 'turn_end') ended = true;
        onEvent(event);
      }
    }
  } catch {
    return false;
  }
}

function parseFrame(frame: string): AgentEvent | null {
  if (frame.startsWith(':')) return null; // heartbeat comment
  let seq = 0;
  let type = 'message';
  let data = '';
  for (const line of frame.split('\n')) {
    if (line.startsWith('id: ')) seq = Number(line.slice(4));
    else if (line.startsWith('event: ')) type = line.slice(7);
    else if (line.startsWith('data: ')) data += line.slice(6);
  }
  return { seq, type, data: data ? JSON.parse(data) : {} };
}

/**
 * Sends a turn and streams it. If the connection drops before turn_end, the turn keeps running on the agent:
 * resume with GET + Last-Event-ID and pick up exactly where the stream stopped.
 */
export async function runTurn(
  token: string,
  conversationId: string,
  body: TurnBody,
  cursor: Cursor,
  onEvent: OnEvent,
  onResume: (afterSeq: number) => void,
): Promise<void> {
  const path = `/api/agent/conversations/${conversationId}`;
  let ok = await readStream(
    await request(token, path, { method: 'POST', body: JSON.stringify(body) }),
    cursor,
    onEvent,
  );
  for (let attempt = 1; !ok && attempt <= 3; attempt++) {
    onResume(cursor.lastSeq);
    await new Promise((r) => setTimeout(r, 500 * attempt));
    try {
      const res = await request(token, path, { method: 'GET', headers: { 'last-event-id': String(cursor.lastSeq) } });
      ok = await readStream(res, cursor, onEvent);
    } catch (err) {
      if (err instanceof HttpError) throw err;
      ok = false;
    }
  }
}

/** Every stored event of the conversation, from the start (and live ones if a turn is running). */
export async function replay(token: string, conversationId: string, cursor: Cursor, onEvent: OnEvent): Promise<void> {
  await readStream(await request(token, `/api/agent/conversations/${conversationId}`), cursor, onEvent);
}
