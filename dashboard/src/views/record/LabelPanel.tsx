import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/errors';
import { fmtDuration } from '../../lib/format';
import { useNow } from '../../lib/useNow';
import { useSnapshot, useTransport } from '../../state/store';

const MAX_LABEL = 50; // labels stay numeric 0–50, 0 = baseline (SPEC non-goals)
const LABELS = Array.from({ length: MAX_LABEL }, (_, i) => i + 1); // 1–50; 0 has its own row

function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

/**
 * Label panel (SPEC §5.2): the active label huge with its own elapsed time, a
 * 0–50 grid of large buttons, and keyboard entry — digits accumulate in an
 * on-screen buffer, Enter applies, Esc returns to baseline 0, Backspace edits.
 * Shortcuts are disabled while a text input is focused. Changes are optimistic
 * with a visible pending state; failures show inline (never alert()).
 */
export function LabelPanel() {
  const transport = useTransport();
  const label = useSnapshot((s) => s.snapshot?.session?.label ?? null);
  const sessionId = useSnapshot((s) => s.snapshot?.session?.session_id ?? null);
  // Labels already used this session, so the operator can see how far along the protocol is.
  const [visited, setVisited] = useState<Set<number>>(() => new Set());
  const [buffer, setBuffer] = useState('');
  const [pending, setPending] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const now = useNow(1000);
  const pendingRef = useRef(pending);
  pendingRef.current = pending;

  const applyRef = useRef((_id: number) => {});
  applyRef.current = (id: number) => {
    if (!Number.isInteger(id) || id < 0 || id > MAX_LABEL) {
      setError(`Labels are 0–${MAX_LABEL}`);
      return;
    }
    setPending(id);
    setError(null);
    setVisited((v) => (v.has(id) ? v : new Set(v).add(id)));
    transport
      .setLabel(id)
      .then(() => {
        // Do not clear here: the snapshot carrying the new label may arrive a moment later,
        // and clearing first briefly showed the previous label again (0 -> 50 flashed 0).
        // The effect below clears pending once the snapshot agrees; this is only a backstop.
        setTimeout(() => {
          if (pendingRef.current === id) setPending(null);
        }, 3000);
      })
      .catch((e) => {
        setPending((p) => (p === id ? null : p));
        setError(errorMessage(e));
      });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return; // shortcuts off while typing
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (/^[0-9]$/.test(e.key)) {
        e.preventDefault();
        setBuffer((b) => (b.length >= 2 ? b : b + e.key));
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        setBuffer((b) => {
          if (b !== '') applyRef.current(parseInt(b, 10));
          return '';
        });
        return;
      }
      if (e.key === 'Escape') {
        setBuffer('');
        applyRef.current(0); // Esc returns to baseline 0
        return;
      }
      if (e.key === 'Backspace') {
        e.preventDefault();
        setBuffer((b) => b.slice(0, -1));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const activeId = label?.id ?? 0;

  useEffect(() => {
    if (pending !== null && activeId === pending) setPending(null);
  }, [activeId, pending]);

  useEffect(() => {
    setVisited(new Set());
  }, [sessionId]);

  useEffect(() => {
    if (label) setVisited((v) => (v.has(label.id) ? v : new Set(v).add(label.id)));
  }, [label]);
  const shownId = pending ?? activeId;
  const elapsed = label ? fmtDuration(now - label.since_ms) : '0:00';

  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-black uppercase tracking-wider text-zinc-400">Label</h2>
          <div className="flex items-baseline gap-3">
            <span
              className={`text-6xl font-black tabular-nums leading-none ${
                pending !== null ? 'animate-pulse text-cyan-300' : 'text-zinc-50'
              }`}
              data-testid="active-label"
            >
              {shownId}
            </span>
            <span className="text-xl font-bold tabular-nums text-zinc-400" data-testid="label-elapsed">
              {elapsed}
            </span>
            {pending !== null && (
              <span className="text-sm font-bold text-cyan-300" role="status">
                pending…
              </span>
            )}
          </div>
        </div>
        <div className="text-right text-sm">
          <span className="text-zinc-500">Keys: </span>
          <span
            className="inline-block min-w-16 rounded border border-zinc-600 bg-zinc-950 px-2 py-0.5 font-black tabular-nums text-cyan-200"
            data-testid="label-buffer"
            aria-label="Keyboard buffer"
          >
            {buffer || '—'}
          </span>
          <span className="ml-1 text-xs text-zinc-500">⏎ apply · Esc → 0 · ⌫ edit</span>
        </div>
      </div>

      {error && (
        <p className="mt-1 text-sm font-bold text-red-400" role="alert" data-testid="label-error">
          {error}
        </p>
      )}

      <div className="mt-2 flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
        <button
          type="button"
          onClick={() => applyRef.current(0)}
          aria-pressed={shownId === 0}
          className={`w-full rounded py-1.5 text-lg font-black uppercase tracking-wider transition-colors ${
            shownId === 0 ? 'bg-cyan-400 text-black' : 'bg-zinc-800 text-zinc-200 hover:bg-zinc-700'
          } ${pending === 0 ? 'opacity-60' : ''}`}
        >
          0 · reset
        </button>
        <div className="grid grid-cols-10 content-start gap-1">
          {LABELS.map((id) => {
            const isActive = shownId === id;
            const done = !isActive && visited.has(id);
            return (
              <button
                key={id}
                type="button"
                onClick={() => applyRef.current(id)}
                aria-pressed={isActive}
                data-visited={done || undefined}
                className={`rounded py-1.5 text-lg font-black tabular-nums transition-colors ${
                  isActive
                    ? 'bg-cyan-400 text-black'
                    : done
                      ? 'bg-emerald-800 text-emerald-100 hover:bg-emerald-700'
                      : 'bg-zinc-950 text-zinc-300 hover:bg-zinc-800'
                } ${pending === id ? 'opacity-60' : ''}`}
              >
                {id}
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
