import { useEffect, useRef, useState } from 'react';
import { useSnapshot } from '../state/store';
import { DoneView } from './DoneView';
import { RecordView } from './RecordView';
import { SetupView } from './SetupView';

/**
 * Home is a SWITCH ON BACKEND STATE, not a menu (SPEC §5): every open
 * browser shows the same screen because the snapshot decides.
 *
 * - RECORDING → Record view, FINALIZING/VALIDATING → Done for that session.
 * - When a session finishes (FINALIZING → IDLE), `/` keeps showing Done for
 *   `last_session_id` until the operator presses "New session". That
 *   acknowledgement lives in component state ONLY — a reload with IDLE state
 *   shows Setup, which is fine because the data is already on disk.
 */
export function Home() {
  const state = useSnapshot((s) => s.snapshot?.state ?? null);
  const session = useSnapshot((s) => s.snapshot?.session ?? null);
  const finalize = useSnapshot((s) => s.snapshot?.finalize ?? null);
  const lastSid = useSnapshot((s) => s.snapshot?.last_session_id ?? null);
  const [justFinished, setJustFinished] = useState(false);
  const prevState = useRef(state);

  useEffect(() => {
    const prev = prevState.current;
    if ((prev === 'FINALIZING' || prev === 'VALIDATING') && state === 'IDLE') {
      setJustFinished(true); // the session we were watching just completed
    }
    prevState.current = state;
  }, [state]);

  if (state === null) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-zinc-400">
        <p className="text-2xl font-black text-zinc-200">IMU Collector</p>
        <p className="text-sm">Connecting to the backend…</p>
      </div>
    );
  }

  if (state === 'RECORDING') return <RecordView />;

  if (state === 'FINALIZING' || state === 'VALIDATING') {
    const sid = session?.session_id ?? finalize?.session_id ?? lastSid;
    if (sid) return <DoneView sid={sid} />;
    return <SetupView />;
  }

  // IDLE / ERROR: Done only while the finished session is unacknowledged.
  if (justFinished && lastSid) {
    return <DoneView sid={lastSid} onNewSession={() => setJustFinished(false)} />;
  }
  return <SetupView />;
}
