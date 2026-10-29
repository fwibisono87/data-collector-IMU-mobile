import { PanelBoundary } from '../components/PanelBoundary';
import { fmtDuration } from '../lib/format';
import { useNow } from '../lib/useNow';
import { useSnapshot } from '../state/store';
import { CameraStrip } from './record/CameraStrip';
import { LabelPanel } from './record/LabelPanel';
import { LiveChartBox } from './record/LiveChart';
import { PhoneStrip } from './record/PhoneStrip';
import { StopButton } from './record/StopButton';

/**
 * Record view (SPEC §5.2). Shown automatically in every open browser while
 * the backend state is RECORDING — the view follows backend state, not
 * clicks. Laid out for 1366×768 with no page scroll.
 */
export function RecordView() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2" data-testid="record-view">
      <PanelBoundary name="Session header">
        <RecordHeader />
      </PanelBoundary>
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_400px] gap-2">
        <div className="flex min-h-0 flex-col gap-2">
          <PanelBoundary name="Labels" className="min-h-0 flex-1">
            <LabelPanel />
          </PanelBoundary>
          <PanelBoundary name="Phone strip">
            <PhoneStrip />
          </PanelBoundary>
          <PanelBoundary name="Camera strip">
            <CameraStrip />
          </PanelBoundary>
        </div>
        <div className="flex min-h-0 flex-col gap-2">
          <PanelBoundary name="Live chart" className="min-h-0 flex-1">
            <LiveChartBox />
          </PanelBoundary>
          <PanelBoundary name="Stop">
            <StopButton />
          </PanelBoundary>
        </div>
      </div>
    </div>
  );
}

function RecordHeader() {
  const session = useSnapshot((s) => s.snapshot?.session ?? null);
  const now = useNow(1000);
  if (!session) return null;
  return (
    <div className="flex items-center gap-4 rounded-lg border border-red-500/40 bg-red-950/30 px-3 py-1.5">
      <span className="flex items-center gap-2 text-xl font-black text-red-400">
        <span className="inline-block h-3.5 w-3.5 animate-pulse rounded-full bg-red-500" aria-hidden />
        REC
      </span>
      <span
        className="text-4xl font-black tabular-nums leading-none text-zinc-50"
        data-testid="elapsed-timer"
      >
        {fmtDuration(now - session.started_at_ms)}
      </span>
      <span className="text-lg font-bold text-zinc-300" data-testid="record-subject">
        {session.subject} <span className="text-zinc-500">·</span> {session.session_tag}
      </span>
      <span className="ml-auto text-xs text-zinc-500" title={session.session_id}>
        {session.session_id}
      </span>
    </div>
  );
}
