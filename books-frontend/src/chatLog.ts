/** The chat log as a reducer: agent events in, log entries out. */
import type { AgentEvent, ConfirmAction, Entry, NewEntry } from './types';

export interface LogState {
  entries: Entry[];
  /** The assistant bubble content_delta events are appending to, if any. */
  openAssistant: number | null;
  nextId: number;
}

export type LogAction =
  | { type: 'reset' }
  | { type: 'add'; entry: { type: 'user' | 'step' | 'error'; text: string } }
  | { type: 'event'; event: AgentEvent; replaying: boolean };

export const initialLog: LogState = { entries: [], openAssistant: null, nextId: 1 };

function push(state: LogState, entry: NewEntry): LogState {
  return { ...state, entries: [...state.entries, { ...entry, id: state.nextId }], nextId: state.nextId + 1 };
}

function step(state: LogState, text: string): LogState {
  return push(state, { type: 'step', text });
}

/** Appends to (or, with replace, overwrites) the open assistant bubble, opening one if needed. */
function assistantText(state: LogState, text: string, replace: boolean): LogState {
  if (state.openAssistant === null) {
    const next = push(state, { type: 'assistant', text });
    return { ...next, openAssistant: state.nextId };
  }
  return {
    ...state,
    entries: state.entries.map((e) =>
      e.id === state.openAssistant && e.type === 'assistant' ? { ...e, text: replace ? text : e.text + text } : e,
    ),
  };
}

function short(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export function chatLog(state: LogState, action: LogAction): LogState {
  if (action.type === 'reset') return initialLog;
  if (action.type === 'add') return push(state, action.entry);

  const { type, data: d } = action.event;
  switch (type) {
    case 'turn_start':
      return step({ ...state, openAssistant: null }, `▶ turn ${d.turnId} (${d.kind})`);
    case 'user_message':
      // Shown when sent; only a replay needs to draw it.
      return action.replaying ? push(state, { type: 'user', text: d.text }) : state;
    case 'tool_call_start':
      return step(state, `→ calling ${d.tool}${d.arguments ? ' ' + JSON.stringify(d.arguments) : ''}`);
    case 'tool_call_end':
      return step(state, `← ${d.tool}: ${short(d.result)}`.slice(0, 300));
    case 'tool_call_error':
      return push(state, { type: 'error', text: `✕ ${d.tool}: ${d.error}` });
    case 'content_delta':
      return assistantText(state, d.text, false);
    case 'assistant_message':
      return assistantText(state, d.text, true);
    case 'confirm_required':
      return push(state, { type: 'confirm', action: d as ConfirmAction });
    case 'confirm_received':
      return step(state, `✓ you chose ${d.decision}`);
    case 'error':
      return push(state, { type: 'error', text: d.message });
    case 'turn_end':
      return step(
        state,
        `■ turn ended: ${d.status}${d.checkpointVersion ? ' · checkpoint v' + d.checkpointVersion : ''}`,
      );
    default:
      return state;
  }
}
