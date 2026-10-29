import { useState } from 'react';
import { SCENARIOS } from '../api/mock';
import { useMockContext } from './mockContext';
import { useSnapshot } from '../state/store';

/**
 * Floating dev-only panel: pick one of the 8 mock scenarios. Switching a
 * scenario rebuilds the MockTransport (main.tsx keys createApp on it), so
 * every scenario starts from a clean in-memory backend. Never rendered in
 * production builds (MockContext is null there).
 */
export function DevPanel() {
  const mock = useMockContext();
  const [open, setOpen] = useState(false);
  const connected = useSnapshot((s) => s.connected);
  if (!mock) return null;

  return (
    <div className="fixed bottom-3 right-3 z-50" data-testid="dev-panel">
      {open && (
        <div className="mb-2 w-80 rounded-lg border border-fuchsia-500/40 bg-zinc-950/95 p-2 shadow-2xl">
          <p className="mb-1 text-[11px] font-black uppercase tracking-wider text-fuchsia-300">
            Mock scenario
          </p>
          <div className="flex flex-col gap-0.5">
            {SCENARIOS.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => {
                  mock.setScenario(s.id);
                  setOpen(false);
                }}
                className={`rounded px-2 py-1 text-left text-xs font-semibold ${
                  s.id === mock.scenario
                    ? 'bg-fuchsia-500/20 text-fuchsia-200'
                    : 'text-zinc-300 hover:bg-zinc-800'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
          <p className="mt-1.5 text-[10px] text-zinc-500">
            socket: {connected ? 'connected' : 'down'} · switching rebuilds the mock backend
          </p>
        </div>
      )}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Mock scenarios"
        className="ml-auto block rounded-full border border-fuchsia-500/60 bg-fuchsia-950/80 px-3 py-1.5 text-xs font-black text-fuchsia-200 shadow-lg hover:bg-fuchsia-900"
      >
        {open ? 'close ✕' : `mock · ${mock.scenario}`}
      </button>
    </div>
  );
}
