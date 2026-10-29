import { useCallback, useEffect, useRef } from 'react';

/**
 * Trailing-edge debounce with flush/cancel, for the 400 ms draft autosave
 * (SPEC §5.1). The callback ref updates every render, so callers can close
 * over fresh props without re-arming the timer.
 */
export function useDebounced<A extends unknown[]>(fn: (...args: A) => void, delayMs: number) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<A | null>(null);

  const cancel = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    pending.current = null;
  }, []);

  const schedule = useCallback(
    (...args: A) => {
      pending.current = args;
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        timer.current = null;
        const a = pending.current;
        pending.current = null;
        if (a) fnRef.current(...a);
      }, delayMs);
    },
    [delayMs],
  );

  const flush = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    const a = pending.current;
    pending.current = null;
    if (a) fnRef.current(...a);
  }, []);

  useEffect(() => cancel, [cancel]);

  return { schedule, flush, cancel };
}
