import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/errors';
import { useTransport } from '../../state/store';

const HOLD_MS = 1000;

type Arm = 'idle' | 'holding' | 'sent';

/**
 * STOP is a 1 s press-and-hold with a fill animation (SPEC §5.2), also driven
 * by holding Space. Releasing early does nothing; completing the hold sends
 * exactly one POST /api/session/stop; after that the button is disarmed, so a
 * double hold can never send twice. On success the snapshot flips to
 * FINALIZING and the router swaps in the Done view.
 */
export function StopButton() {
  const transport = useTransport();
  const [progress, setProgress] = useState(0);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const armRef = useRef<Arm>('idle');
  const startRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  const complete = () => {
    if (armRef.current !== 'holding') return;
    armRef.current = 'sent';
    clearTimer();
    setProgress(1);
    setStopping(true);
    setError(null);
    transport.stopSession().catch((e) => {
      // STOP refused (e.g. backend already stopped it): re-arm and show why.
      armRef.current = 'idle';
      setStopping(false);
      setProgress(0);
      setError(errorMessage(e));
    });
  };

  const begin = () => {
    if (armRef.current !== 'idle') return;
    armRef.current = 'holding';
    startRef.current = Date.now();
    clearTimer();
    timerRef.current = setInterval(() => {
      const p = Math.min(1, (Date.now() - startRef.current) / HOLD_MS);
      setProgress(p);
      if (p >= 1) complete();
    }, 30);
  };

  const release = () => {
    if (armRef.current !== 'holding') return; // released after completion: no-op
    armRef.current = 'idle';
    clearTimer();
    setProgress(0);
  };

  // Hold Space anywhere in the Record view (but not while typing somewhere).
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code !== 'Space' && e.key !== ' ') return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName?.toUpperCase();
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return;
      e.preventDefault(); // no page scroll, no native button activation
      if (!e.repeat) begin();
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== 'Space' && e.key !== ' ') return;
      release();
    };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => clearTimer, []);

  return (
    <section className="shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
      <button
        type="button"
        data-testid="stop-button"
        aria-label="Stop recording: press and hold for 1 second"
        disabled={stopping}
        onPointerDown={(e) => {
          e.preventDefault();
          begin();
        }}
        onPointerUp={release}
        onPointerLeave={release}
        onContextMenu={(e) => e.preventDefault()}
        className={`relative h-20 w-full select-none overflow-hidden rounded-lg border-2 text-2xl font-black uppercase tracking-widest ${
          stopping
            ? 'cursor-wait border-red-800 bg-red-950 text-red-300'
            : 'border-red-500 bg-red-950 text-red-100 hover:border-red-400 active:cursor-grabbing'
        }`}
      >
        <span
          aria-hidden
          className="absolute inset-y-0 left-0 bg-red-600/80"
          style={{ width: `${Math.round(progress * 100)}%` }}
          data-testid="stop-fill"
        />
        <span className="relative">
          {stopping ? 'Stopping…' : progress > 0 ? 'Keep holding…' : 'Hold to stop · 1 s'}
        </span>
      </button>
      <p className="mt-1 text-center text-[11px] text-zinc-500">or hold Space</p>
      {error && (
        <p className="mt-1 text-sm font-bold text-red-400" role="alert" data-testid="stop-error">
          {error}
        </p>
      )}
    </section>
  );
}
