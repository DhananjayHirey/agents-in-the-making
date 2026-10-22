export interface User {
  name: string;
  role: string;
}

/** One SSE event from the agent: `seq` is the event id, used to resume with Last-Event-ID. */
export interface AgentEvent {
  seq: number;
  type: string;
  data: Record<string, any>;
}

export interface ConfirmAction {
  actionId: string;
  expiresAt: string;
  preview: { summary: string; before: unknown; after: unknown };
}

/** The body of POST /api/agent/conversations/:id — one endpoint takes every turn. */
export type TurnBody =
  | { kind: 'message'; turnId: string; text: string }
  | { kind: 'confirm'; turnId: string; actionId: string; decision: 'approve' | 'reject' };

/** A line in the chat log, before the log gives it an id. */
export type NewEntry =
  | { type: 'user' | 'assistant' | 'step' | 'error'; text: string }
  | { type: 'confirm'; action: ConfirmAction };

export type Entry = NewEntry & { id: number };
