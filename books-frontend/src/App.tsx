import { useCallback, useReducer, useRef, useState, type FormEvent } from 'react';
import * as api from './api';
import { chatLog, initialLog } from './chatLog';
import { ChatLog } from './components/ChatLog';
import { SignIn } from './components/SignIn';
import type { AgentEvent, TurnBody, User } from './types';

export function App() {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState('Not signed in');
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [log, dispatch] = useReducer(chatLog, initialLog);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  // The last event id seen. A ref, because the stream readers update it mid-turn; lastSeq mirrors it for display.
  const cursor = useRef<api.Cursor>({ lastSeq: 0 });
  const [lastSeq, setLastSeq] = useState(0);

  const onEvent = useCallback((event: AgentEvent, replaying = false) => {
    setLastSeq(cursor.current.lastSeq);
    dispatch({ type: 'event', event, replaying });
  }, []);

  const showError = (err: unknown) =>
    dispatch({ type: 'add', entry: { type: 'error', text: err instanceof Error ? err.message : String(err) } });

  async function newConversation(sessionToken = token) {
    if (!sessionToken) return;
    try {
      const id = await api.createConversation(sessionToken);
      setConversationId(id);
      cursor.current = { lastSeq: 0 };
      setLastSeq(0);
      dispatch({ type: 'reset' });
    } catch (err) {
      showError(err);
    }
  }

  async function signIn(email: string, password: string) {
    try {
      const session = await api.login(email, password);
      setToken(session.token);
      setUser(session.user);
      setStatus(`Signed in as ${session.user.name} (${session.user.role})`);
      await newConversation(session.token);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    }
  }

  async function turn(body: TurnBody) {
    if (!token || !conversationId) return;
    setBusy(true);
    try {
      await api.runTurn(token, conversationId, body, cursor.current, onEvent, (after) =>
        dispatch({ type: 'add', entry: { type: 'step', text: `↻ connection lost, resuming after event ${after}` } }),
      );
    } catch (err) {
      showError(err);
    } finally {
      setBusy(false);
    }
  }

  async function send(e: FormEvent) {
    e.preventDefault();
    const message = text.trim();
    if (!message) return;
    setText('');
    dispatch({ type: 'add', entry: { type: 'user', text: message } });
    // The client makes the turnId, so a network retry of the same POST is recognised and not run twice.
    await turn({ kind: 'message', turnId: `t_${crypto.randomUUID()}`, text: message });
  }

  const decide = (actionId: string, decision: 'approve' | 'reject') =>
    turn({ kind: 'confirm', turnId: `t_${crypto.randomUUID()}`, actionId, decision });

  async function replayFromStorage() {
    if (!token || !conversationId) return;
    dispatch({ type: 'reset' });
    cursor.current = { lastSeq: 0 };
    try {
      await api.replay(token, conversationId, cursor.current, (event) => onEvent(event, true));
    } catch (err) {
      showError(err);
    }
  }

  const ready = Boolean(conversationId);

  return (
    <div className="wrap">
      <h1>Books Agent POC — browser → backend relay → agent → backend GraphQL</h1>

      <div className="card">
        <ChatLog entries={log.entries} onDecide={decide} />
        <form className="row" onSubmit={send}>
          <input
            placeholder="e.g. Which sci-fi books do we have? / Change Dune's year to 1966 / Delete Clean Code"
            autoComplete="off"
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={!ready}
          />
          <button disabled={!ready || busy}>Send</button>
        </form>
      </div>

      <div className="card">
        <SignIn onSignIn={signIn} />
        <div className="row">
          <button className="ghost" disabled={!user} onClick={() => newConversation()}>
            New chat
          </button>
        </div>
        <p className="meta">{status}</p>
        <p className="meta">{conversationId && `conversation ${conversationId}`}</p>
        <p className="meta">Last event seq: {lastSeq}</p>
        <div className="row">
          <button className="ghost" disabled={!ready} onClick={replayFromStorage}>
            Replay from storage
          </button>
        </div>
      </div>
    </div>
  );
}
