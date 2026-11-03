import { useApp, useStoreValue } from '../state/store';
import type { Drift } from '../api/types';

const VERDICT_LABEL: Record<Drift['verdict'], string> = {
  OK: 'OK',
  CHECK: 'CHECK',
  MOVED: 'MOVED',
  NO_SIGNAL: 'NO SIGNAL',
  MISSING: 'MISSING',
  RESOLUTION_CHANGED: 'RESOLUTION CHANGED',
};

const VERDICT_STYLE: Record<Drift['verdict'], string> = {
  OK: 'bg-green-500/15 text-green-300 border-green-500/40',
  CHECK: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  MOVED: 'bg-red-500/15 text-red-300 border-red-500/40',
  NO_SIGNAL: 'bg-zinc-500/15 text-zinc-300 border-zinc-500/40',
  MISSING: 'bg-zinc-500/15 text-zinc-300 border-zinc-500/40',
  RESOLUTION_CHANGED: 'bg-red-500/15 text-red-300 border-red-500/40',
};

const ARROWS: Record<string, string> = {
  '1,0': '→',
  '-1,0': '←',
  '0,1': '↓',
  '0,-1': '↑',
  '1,1': '↘',
  '1,-1': '↗',
  '-1,1': '↙',
  '-1,-1': '↖',
};

/** Arrow pointing in the direction the image content has shifted. */
export function shiftArrow(dx: number, dy: number): string {
  const sx = Math.abs(dx) >= 1 ? Math.sign(dx) : 0;
  const sy = Math.abs(dy) >= 1 ? Math.sign(dy) : 0;
  if (sx === 0 && sy === 0) return '·';
  return ARROWS[`${sx},${sy}`] ?? '·';
}

/**
 * Calibration verdict badge. Subscribes to the isolated drift store (fed by
 * 2 Hz `calibration.drift` messages), so drift updates re-render only this
 * badge — never the surrounding view. Falls back to the `drift` field the
 * snapshot carries per camera.
 */
export function VerdictBadge({
  deviceKey,
  camId,
  fallback,
}: {
  deviceKey: string;
  camId?: string;
  fallback?: Drift | null;
}) {
  const { drift } = useApp();
  const d = useStoreValue(drift, (s) => {
    const cams = s?.cameras;
    if (!cams) return null;
    return cams.find((c) => c.device_key === deviceKey || (camId && c.cam_id === camId)) ?? null;
  });
  const show = d ?? fallback ?? null;
  if (!show) return null;
  const style = VERDICT_STYLE[show.verdict] ?? VERDICT_STYLE.MISSING;
  const label = VERDICT_LABEL[show.verdict] ?? String(show.verdict);
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs font-black tabular-nums ${style}`}
      data-testid="verdict-badge"
      title={`score ${show.score?.toFixed?.(2) ?? '?'} · confidence ${show.confidence?.toFixed?.(2) ?? '?'}`}
    >
      {label}
      <span className="font-semibold">
        Δx {Math.round(show.dx_px)} Δy {Math.round(show.dy_px)} {shiftArrow(show.dx_px, show.dy_px)}
      </span>
    </span>
  );
}
