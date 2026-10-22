import { useState } from 'react';
import type { ConfirmAction } from '../types';

interface Props {
  action: ConfirmAction;
  onDecide: (actionId: string, decision: 'approve' | 'reject') => Promise<void>;
}

/** A write the agent proposed: shows before/after and waits for the user's Confirm or Cancel. */
export function ConfirmCard({ action, onDecide }: Props) {
  const [decided, setDecided] = useState(false);
  const { preview } = action;

  const decide = async (decision: 'approve' | 'reject') => {
    setDecided(true);
    await onDecide(action.actionId, decision);
  };

  return (
    <div className="confirm">
      <strong>{preview.summary}</strong>
      <pre>
        {`before: ${JSON.stringify(preview.before, null, 1)}\nafter:  ${JSON.stringify(preview.after, null, 1)}\nexpires: ${action.expiresAt}`}
      </pre>
      <div className="row">
        <button disabled={decided} onClick={() => decide('approve')}>
          Confirm
        </button>
        <button className="ghost" disabled={decided} onClick={() => decide('reject')}>
          Cancel
        </button>
      </div>
    </div>
  );
}
