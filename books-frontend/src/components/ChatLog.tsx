import { useEffect, useRef } from 'react';
import type { Entry } from '../types';
import { ConfirmCard } from './ConfirmCard';

interface Props {
  entries: Entry[];
  onDecide: (actionId: string, decision: 'approve' | 'reject') => Promise<void>;
}

const CLASS: Record<Exclude<Entry['type'], 'confirm'>, string> = {
  user: 'msg user',
  assistant: 'msg assistant',
  step: 'step',
  error: 'msg assistant error',
};

export function ChatLog({ entries, onDecide }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [entries]);

  return (
    <div className="log" ref={ref} aria-live="polite">
      {entries.map((e) =>
        e.type === 'confirm' ? (
          <ConfirmCard key={e.id} action={e.action} onDecide={onDecide} />
        ) : (
          <div key={e.id} className={CLASS[e.type]}>
            {e.text}
          </div>
        ),
      )}
    </div>
  );
}
