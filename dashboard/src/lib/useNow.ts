import { useEffect, useState } from 'react';

/**
 * Re-renders ONLY the calling component at a fixed interval, for elapsed
 * timers. Nothing else in the tree re-renders when the clock ticks.
 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
