import { Link } from 'react-router-dom';
import { useSnapshot } from '../state/store';
import { EMPTY_CAMERAS, EMPTY_PHONES } from '../lib/empty';
import { fmtBytes, fmtDuration } from '../lib/format';
import { useNow } from '../lib/useNow';
import type { Snapshot } from '../api/types';

const STATE_STYLE: Record<Snapshot['state'], string> = {
  IDLE: 'bg-zinc-700 text-zinc-100',
  RECORDING: 'bg-red-600 text-white',
  FINALIZING: 'bg-amber-500 text-black',
  VALIDATING: 'bg-amber-500 text-black',
  ERROR: 'bg-red-700 text-white',
};

/**
 * Always visible: state pill (with elapsed while recording), subject/tag,
 * phones online, cameras live/selected, disk free, links and the build id
 * (stale-bundle skew must be visible at a glance — SPEC §1.1).
 */
export function TopBar() {
  const state = useSnapshot((s) => s.snapshot?.state ?? null);
  const session = useSnapshot((s) => s.snapshot?.session ?? null);
  const phones = useSnapshot((s) => s.snapshot?.phones ?? EMPTY_PHONES);
  const cameras = useSnapshot((s) => s.snapshot?.cameras ?? EMPTY_CAMERAS);
  const disk = useSnapshot((s) => s.snapshot?.disk ?? null);
  const backendBuild = useSnapshot((s) => s.snapshot?.build_id ?? null);
  const now = useNow(1000);

  const onlinePhones = phones.filter((p) => p.online).length;
  const liveCams = cameras.filter((c) => c.state === 'live').length;
  const elapsed =
    state === 'RECORDING' && session ? fmtDuration(now - session.started_at_ms) : null;

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-900 px-3 text-sm">
      <Link to="/" className="font-black tracking-tight text-zinc-100">
        IMU&nbsp;Collector
      </Link>

      <span
        className={`rounded-full px-3 py-1 text-sm font-black tabular-nums ${
          state ? STATE_STYLE[state] : 'bg-zinc-800 text-zinc-400'
        } ${state === 'RECORDING' ? 'animate-pulse' : ''}`}
        data-testid="state-pill"
      >
        {state === null ? '…' : state === 'RECORDING' ? `REC ${elapsed}` : state}
      </span>

      {state === 'RECORDING' && session && (
        <span className="max-w-64 truncate font-semibold text-zinc-200" data-testid="topbar-session">
          {session.subject} · {session.session_tag}
        </span>
      )}

      <div className="ml-auto flex items-center gap-4 tabular-nums text-zinc-300">
        <span title="Phones online">
          <span className="font-bold text-zinc-100" data-testid="phones-count">
            {onlinePhones}/{phones.length}
          </span>{' '}
          phones
        </span>
        <span title="Cameras live / selected">
          <span className="font-bold text-zinc-100" data-testid="cams-count">
            {liveCams}/{cameras.length}
          </span>{' '}
          cams
        </span>
        <span title={disk ? `Free on ${disk.path}` : 'Disk free'} data-testid="disk-free">
          {fmtBytes(disk?.free_bytes ?? null)} free
        </span>
        <nav className="flex items-center gap-3 font-semibold">
          <Link to="/sessions" className="text-cyan-300 hover:text-cyan-200 hover:underline">
            Sessions
          </Link>
          <Link to="/calibration" className="text-cyan-300 hover:text-cyan-200 hover:underline">
            Calibration
          </Link>
        </nav>
        <span
          className="text-xs text-zinc-500"
          title={backendBuild ? `backend build ${backendBuild}` : undefined}
          data-testid="build-id"
        >
          build {__BUILD_ID__}
        </span>
      </div>
    </header>
  );
}
