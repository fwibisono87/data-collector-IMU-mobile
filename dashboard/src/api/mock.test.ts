import { afterEach, describe, expect, it, vi } from 'vitest';
import { MockTransport, SCENARIOS, type ScenarioId } from './mock';
import type { ServerMessage, Snapshot, SnapshotMsg } from './types';

/**
 * MockTransport is the contract stand-in for `npm run dev:mock`: these tests
 * pin the scripted scenarios and the readiness/command semantics so the mock
 * cannot silently drift from API.md while lanes L4/L5 build the real backend.
 */

const created: MockTransport[] = [];

afterEach(() => {
  while (created.length > 0) created.pop()?.dispose();
  vi.useRealTimers();
});

function make(scenario: ScenarioId): { t: MockTransport; msgs: ServerMessage[] } {
  const t = new MockTransport(scenario, { latencyMs: 0 });
  created.push(t);
  const msgs: ServerMessage[] = [];
  t.subscribe((m) => msgs.push(m));
  return { t, msgs };
}

function lastSnapshot(msgs: ServerMessage[]): Snapshot {
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const m = msgs[i];
    if (m && m.type === 'snapshot') return (m as SnapshotMsg).data;
  }
  throw new Error('no snapshot emitted');
}

async function tick(times = 4): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}

describe('scenarios', () => {
  it('ships the 8 documented scenarios', () => {
    expect(SCENARIOS).toHaveLength(8);
    expect(new Set(SCENARIOS.map((s) => s.id)).size).toBe(8);
  });

  it('1 · idle: 3 phones (one at 72 Hz), 2 cameras, empty draft → blocked with a low-rate warning', async () => {
    const { t, msgs } = make('idle');
    await tick();
    const snap = lastSnapshot(msgs);
    expect(snap.state).toBe('IDLE');
    expect(snap.phones).toHaveLength(3);
    expect(snap.cameras).toHaveLength(2);
    expect(snap.draft.subject).toBe('');
    expect(snap.phones.some((p) => p.true_hz_avg < 90)).toBe(true);
    // SPEC §5.1 readiness: empty draft blocks, low rate warns.
    const codes = snap.readiness.blockers.map((b) => b.code);
    expect(codes).toContain('draft_subject');
    expect(snap.readiness.warnings.map((w) => w.code)).toContain('low_rate');
    await expect(t.startSession()).rejects.toMatchObject({ status: 409, code: 'blocked' });
    t.dispose();
  });

  it('2 · ready: start → label → stop, stop is idempotent', async () => {
    const { t, msgs } = make('ready');
    await tick();
    expect(lastSnapshot(msgs).readiness.blockers).toHaveLength(0);
    const start = await t.startSession();
    expect(start.session_id).toBeTruthy();
    expect(lastSnapshot(msgs).state).toBe('RECORDING');

    const label = await t.setLabel(7);
    expect(label.label_id).toBe(7);
    await expect(t.setLabel(51)).rejects.toMatchObject({ status: 422 });

    const stop = await t.stopSession();
    expect(stop.session_id).toBe(start.session_id);
    expect(lastSnapshot(msgs).state).toBe('FINALIZING');
    // API.md: STOP is idempotent shortly after success.
    const stop2 = await t.stopSession();
    expect(stop2.session_id).toBe(start.session_id);
    t.dispose();
  });

  it('3 · recording: scripted labels and a phone dropping at 12 s', async () => {
    vi.useFakeTimers();
    const { t, msgs } = make('recording');
    await vi.advanceTimersByTimeAsync(10);
    expect(lastSnapshot(msgs).state).toBe('RECORDING');

    await vi.advanceTimersByTimeAsync(4_100);
    expect(lastSnapshot(msgs).session?.label.id).toBe(3);
    await vi.advanceTimersByTimeAsync(5_100); // t=9.2 s
    expect(lastSnapshot(msgs).session?.label.id).toBe(12);

    await vi.advanceTimersByTimeAsync(3_000); // t=12.2 s: waist drops
    const snap = lastSnapshot(msgs);
    const waist = snap.phones.find((p) => p.role === 'waist');
    expect(waist?.online).toBe(false);
    expect(waist?.buffering).toBe(true);
    t.dispose();
  });

  it('4 · finalizing: steps progress and the session lands on disk', async () => {
    vi.useFakeTimers();
    const { t, msgs } = make('finalizing');
    await vi.advanceTimersByTimeAsync(1_900); // finalize begins at 1 s, step 1 runs at 1.8 s
    expect(lastSnapshot(msgs).state).toBe('FINALIZING');
    const running = lastSnapshot(msgs).finalize?.steps.some((s) => s.status === 'running');
    expect(running).toBe(true);

    await vi.advanceTimersByTimeAsync(15_000); // the whole script (~10 s)
    const snap = lastSnapshot(msgs);
    expect(snap.state).toBe('IDLE');
    const sid = snap.last_session_id;
    expect(sid).toBeTruthy();
    const detail = await t.getSession(sid as string);
    expect(detail.verdict).toBe('PASS');
    expect(detail.zip_path).toBeTruthy();
    t.dispose();
  });

  it('5 · done-pass: PASS verdict with ZIP and full integrity devices', async () => {
    const { t, msgs } = make('done-pass');
    await tick();
    const snap = lastSnapshot(msgs);
    expect(snap.last_session_id).toBe('mock-0005');
    const detail = await t.getSession('mock-0005');
    expect(detail.verdict).toBe('PASS');
    expect(detail.zip_path).toBeTruthy();
    const report = detail.integrity_report as { devices?: Array<{ status: string }> };
    expect(report.devices?.length).toBe(3);
    const list = await t.listSessions();
    expect(list.sessions.length).toBeGreaterThanOrEqual(3); // includes history rows
    t.dispose();
  });

  it('6 · done-partial: PARTIAL verdict, late rows and notices', async () => {
    const { t, msgs } = make('done-partial');
    await tick();
    const snap = lastSnapshot(msgs);
    expect(snap.notices.length).toBe(2);
    const detail = await t.getSession('mock-0006');
    expect(detail.verdict).toBe('PARTIAL');
    expect(detail.late).toHaveLength(1);
    expect(detail.uploads[0]?.complete).toBe(false);
    // Dismiss is server-side: the next snapshot no longer carries the notice.
    await t.dismissNotice('n-late-1');
    expect(lastSnapshot(msgs).notices.map((n) => n.id)).not.toContain('n-late-1');
    t.dispose();
  });

  it('7 · camera-lost: cam2 is lost at 4 s and back at 11 s, recording continues', async () => {
    vi.useFakeTimers();
    const { t, msgs } = make('camera-lost');
    await vi.advanceTimersByTimeAsync(10);
    expect(lastSnapshot(msgs).state).toBe('RECORDING');

    await vi.advanceTimersByTimeAsync(4_100);
    let snap = lastSnapshot(msgs);
    expect(snap.cameras.find((c) => c.cam_id === 'cam2')?.state).toBe('lost');
    expect(snap.state).toBe('RECORDING'); // the session survives a lost camera
    expect(snap.notices.some((n) => n.code === 'camera_lost')).toBe(true);

    await vi.advanceTimersByTimeAsync(7_100);
    snap = lastSnapshot(msgs);
    expect(snap.cameras.find((c) => c.cam_id === 'cam2')?.state).toBe('live');
    expect(snap.notices.some((n) => n.code === 'camera_lost')).toBe(false); // auto-resolved
    t.dispose();
  });

  it('8 · calibration: two references, cam2 verdict MOVED, watch pushes drift', async () => {
    const { t, msgs } = make('calibration');
    await tick();
    const refs = await t.listReferences();
    expect(refs.references).toHaveLength(2);
    expect(refs.active_ref_id).toBeTruthy();

    const cmp = await t.compare('active');
    const cam2 = cmp.cameras.find((c) => c.cam_id === 'cam2');
    expect(cam2?.verdict).toBe('MOVED');

    await t.watchCalibration('active');
    const driftMsgs = msgs.filter((m) => m.type === 'calibration.drift');
    expect(driftMsgs.length).toBeGreaterThanOrEqual(1); // pushed immediately on watch
    await t.unwatchCalibration();
    t.dispose();
  });
});

describe('command semantics (API.md)', () => {
  it('setLabel outside recording → 409 bad_state', async () => {
    const { t } = make('idle');
    await expect(t.setLabel(1)).rejects.toMatchObject({ status: 409, code: 'bad_state' });
    t.dispose();
  });

  it('unknown session → 404', async () => {
    const { t } = make('idle');
    await expect(t.getSession('nope')).rejects.toMatchObject({ status: 404 });
    t.dispose();
  });

  it('draft updates flow into the next snapshot and unblock START', async () => {
    const { t, msgs } = make('idle');
    await t.putDraft({ subject: 'S099', session_tag: 'walk', operator: 'ana' });
    const snap = lastSnapshot(msgs);
    expect(snap.draft.subject).toBe('S099');
    expect(snap.readiness.blockers.map((b) => b.code)).not.toContain('draft_subject');
    const start = await t.startSession();
    expect(start.session_id).toBeTruthy();
    t.dispose();
  });

  it('camera selection refuses more than 5 cameras', async () => {
    const { t } = make('ready');
    const six = Array.from({ length: 6 }, (_, i) => ({ device_key: `fake:${i}` }));
    await expect(t.putCameraSelection(six)).rejects.toMatchObject({ status: 422 });
    t.dispose();
  });
});
