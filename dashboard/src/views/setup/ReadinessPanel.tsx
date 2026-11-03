import { useEffect, useState } from 'react';
import { errorMessage, isApiError } from '../../api/errors';
import type { Issue } from '../../api/types';
import { useSnapshot, useTransport } from '../../state/store';

/**
 * Readiness (SPEC §5.1) — one list computed by the BACKEND (single source of
 * truth). Blockers in red disable START; warnings in amber flip the label to
 * "Start anyway". A 409 `blocked` from START shows the returned blockers
 * inline until the next snapshot replaces readiness.
 */
export function ReadinessPanel() {
  const transport = useTransport();
  const readiness = useSnapshot((s) => s.snapshot?.readiness ?? null);
  const state = useSnapshot((s) => s.snapshot?.state ?? null);
  const [refused, setRefused] = useState<Issue[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  // A fresh snapshot means the backend recomputed readiness; drop the stale 409 list.
  useEffect(() => {
    setRefused(null);
  }, [readiness]);

  const blockers = refused ?? readiness?.blockers ?? [];
  const warnings = readiness?.warnings ?? [];
  const busy = state === 'RECORDING' || state === 'FINALIZING' || state === 'VALIDATING';
  const disabled = blockers.length > 0 || pending || busy;
  const label = blockers.length > 0 ? 'Start session' : warnings.length > 0 ? 'Start anyway' : 'Start session';

  const start = () => {
    setPending(true);
    setError(null);
    setRefused(null);
    transport
      .startSession()
      .catch((e) => {
        if (isApiError(e) && e.status === 409 && e.code === 'blocked' && e.blockers) {
          setRefused(e.blockers as Issue[]);
          setError(e.message);
        } else {
          setError(errorMessage(e));
        }
      })
      .finally(() => setPending(false));
    // On success we do nothing: the snapshot flips the state to RECORDING and
    // the router shows the Record view in every open browser.
  };

  return (
    <section className="shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-black uppercase tracking-wider text-zinc-400">Readiness</h2>
          {blockers.length === 0 && warnings.length === 0 && (
            <p className="mt-1 text-sm font-semibold text-green-400">Ready to record.</p>
          )}
          {blockers.length > 0 && (
            <ul className="mt-1 space-y-0.5" data-testid="blockers">
              {blockers.map((b, i) => (
                <li key={`${b.code}-${i}`} className="text-sm font-bold text-red-400">
                  ✗ {b.message}
                </li>
              ))}
            </ul>
          )}
          {warnings.length > 0 && (
            <ul className="mt-1 space-y-0.5" data-testid="warnings">
              {warnings.map((w, i) => (
                <li key={`${w.code}-${i}`} className="text-sm font-semibold text-amber-300">
                  ⚠ {w.message}
                </li>
              ))}
            </ul>
          )}
          {error && refused === null && (
            <p className="mt-1 text-sm font-semibold text-red-400" role="alert">
              {error}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={start}
          disabled={disabled}
          data-testid="start-button"
          className={`h-20 w-56 shrink-0 rounded-lg text-2xl font-black uppercase tracking-wide transition-colors ${
            disabled
              ? 'cursor-not-allowed bg-zinc-800 text-zinc-600'
              : warnings.length > 0
                ? 'bg-amber-500 text-black hover:bg-amber-400'
                : 'bg-green-600 text-white hover:bg-green-500'
          }`}
        >
          {pending ? 'Starting…' : label}
        </button>
      </div>
    </section>
  );
}
