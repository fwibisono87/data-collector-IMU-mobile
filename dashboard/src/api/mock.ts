import { ApiError } from './errors';
import type { Transport } from './transport';
import type {
  Camera,
  CameraDevice,
  CameraSelectionItem,
  CameraSelectionResponse,
  CompareCamera,
  CompareResult,
  DeviceList,
  Draft,
  DraftUpdate,
  Drift,
  FinalizeProgress,
  FinalizeStep,
  Issue,
  LabelResponse,
  Notice,
  PackageResponse,
  Phone,
  Reference,
  ReferenceCreate,
  ReferenceCamera,
  ReferenceList,
  ServerMessage,
  SessionDetail,
  SessionList,
  SessionSummary,
  Snapshot,
  StartResponse,
  StopResponse,
  UploadStatus,
} from './types';

/**
 * MockTransport — an in-memory backend with scripted scenarios, used by
 * `npm run dev:mock` while lanes L4/L5 build the real one, and by tests.
 * It implements the readiness rules of SPEC §5.1 and the command semantics
 * of API.md (409 blocked / bad_state, stop idempotency, notice dismissal…).
 * Previews and stills are canvas test patterns (`mock://` URLs).
 */

export type ScenarioId =
  | 'idle'
  | 'ready'
  | 'recording'
  | 'finalizing'
  | 'done-pass'
  | 'done-partial'
  | 'camera-lost'
  | 'calibration';

export const SCENARIOS: Array<{ id: ScenarioId; label: string }> = [
  { id: 'idle', label: '1 · Idle — 3 phones (one 72 Hz), 2 cameras, empty draft' },
  { id: 'ready', label: '2 · Ready to start' },
  { id: 'recording', label: '3 · Recording — labels change, a phone drops' },
  { id: 'finalizing', label: '4 · Finalizing — steps progress' },
  { id: 'done-pass', label: '5 · Done PASS with ZIP' },
  { id: 'done-partial', label: '6 · Done PARTIAL — late data notice' },
  { id: 'camera-lost', label: '7 · Camera lost mid-session' },
  { id: 'calibration', label: '8 · Calibration — one camera MOVED' },
];

const STEP_NAMES: FinalizeStep['name'][] = [
  'close_writers',
  'cameras',
  'consolidate',
  'validate',
  'bundle',
  'package',
  'late_window',
];

const DEVICES: CameraDevice[] = [
  { device_key: 'fake:0', label: 'Fake Cam HD (front)', backend: 'fake' },
  { device_key: 'fake:1', label: 'Fake Cam HD (side)', backend: 'fake' },
  { device_key: 'fake:2', label: 'Fake Cam HD (spare)', backend: 'fake' },
];

const PHONE_BASE: Array<{ role: string; id: string; hz: number }> = [
  { role: 'chest', id: 'R5CT7-01-chest', hz: 100.2 },
  { role: 'waist', id: 'R5CT7-02-waist', hz: 99.7 },
  { role: 'thigh_left', id: 'R5CT7-03-thighL', hz: 100.5 },
];

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

function makePhone(role: string, id: string, hz: number): Phone {
  return {
    device_id: id,
    role,
    model: 'Galaxy A56',
    app_version: '3.0.0',
    online: true,
    offline_since_ms: null,
    rate_hz: 100,
    true_hz_avg: hz,
    packets: 42_000,
    telemetry_age_ms: 60,
    buffering: false,
  };
}

function makeCam(camId: string, deviceKey: string, label: string, drift: Drift | null = null): Camera {
  return {
    cam_id: camId,
    device_key: deviceKey,
    label,
    state: 'live',
    recording: false,
    width: 1280,
    height: 720,
    fps: 29.97,
    dropped_frames: 0,
    error: null,
    drift,
  };
}

function driftOf(
  verdict: Drift['verdict'],
  dx: number,
  dy: number,
  score: number,
  confidence: number,
): Drift {
  const shift = (Math.hypot(dx, dy) / 1280) * 100;
  return { verdict, dx_px: dx, dy_px: dy, shift_pct: shift, score, confidence, at_ms: Date.now() };
}

export interface MockTransportOptions {
  /** Simulated REST latency. 0 resolves promises immediately (tests). */
  latencyMs?: number;
}

interface RecordScript {
  labels?: Array<{ atMs: number; id: number }>;
  dropPhoneAtMs?: number;
  dropPhoneRole?: string;
  loseCameraAtMs?: number;
  restoreCameraAtMs?: number;
  loseCamId?: string;
}

export class MockTransport implements Transport {
  private snapshot: Snapshot;
  private readonly devices: CameraDevice[] = clone(DEVICES);
  private readonly sessions = new Map<string, SessionDetail>();
  private references: Reference[] = [];
  private activeRefId: string | null = null;

  private onMessage: ((msg: ServerMessage) => void) | null = null;
  private onStatus: ((connected: boolean) => void) | null = null;

  private readonly timers = new Set<ReturnType<typeof setTimeout>>();
  private readonly intervals = new Set<ReturnType<typeof setInterval>>();
  private readonly latency: number;
  private disposed = false;

  private sessionCounter = 0;
  private lastStop: { atMs: number; body: StopResponse } | null = null;
  private liveTimer: ReturnType<typeof setInterval> | null = null;
  private driftTimer: ReturnType<typeof setInterval> | null = null;
  private watchRefId: string | null = null;
  private readonly droppedPhones = new Set<string>();
  private readonly lostCameras = new Set<string>();

  constructor(
    readonly scenario: ScenarioId = 'idle',
    opts: MockTransportOptions = {},
  ) {
    this.latency = opts.latencyMs ?? 120;
    this.snapshot = this.buildSnapshot(scenario);
    this.setupScenario(scenario);
    // Readiness is derived state: compute it once up-front so the very first
    // snapshot a subscriber receives already carries blockers/warnings.
    this.snapshot.readiness = this.computeReadiness();
  }

  // ---------------------------------------------------------------- plumbing

  private after(ms: number, fn: () => void): void {
    const t = setTimeout(() => {
      this.timers.delete(t);
      if (!this.disposed) fn();
    }, ms);
    this.timers.add(t);
  }

  private every(ms: number, fn: () => void): ReturnType<typeof setInterval> {
    const t = setInterval(() => {
      if (!this.disposed) fn();
    }, ms);
    this.intervals.add(t);
    return t;
  }

  /** Run a REST command with the mock latency; exceptions become rejections. */
  private delay<T>(fn: () => T): Promise<T> {
    if (this.latency <= 0) {
      return new Promise<T>((resolve, reject) => {
        try {
          resolve(fn());
        } catch (e) {
          reject(e);
        }
      });
    }
    return new Promise<T>((resolve, reject) => {
      this.after(this.latency, () => {
        try {
          resolve(fn());
        } catch (e) {
          reject(e);
        }
      });
    });
  }

  private emit(msg: ServerMessage): void {
    this.onMessage?.(msg);
  }

  /** Recompute derived fields and broadcast a fresh snapshot. */
  private update(): void {
    this.snapshot.server_time_ms = Date.now();
    this.snapshot.readiness = this.computeReadiness();
    this.emit({ type: 'snapshot', data: clone(this.snapshot) });
  }

  // ------------------------------------------------------------- readiness

  /** SPEC §5.1: blockers refuse START; warnings only get recorded. */
  private computeReadiness(): { blockers: Issue[]; warnings: Issue[] } {
    const s = this.snapshot;
    const blockers: Issue[] = [];
    const warnings: Issue[] = [];

    if (s.disk.free_bytes < 5e9) {
      blockers.push({
        code: 'disk_space',
        message: `Less than 5 GB free on ${s.disk.path} — point SSD_PATH at an empty disk`,
      });
    } else if (s.disk.free_bytes < 20e9) {
      warnings.push({ code: 'disk_space_low', message: `Less than 20 GB free on ${s.disk.path}` });
    }

    const online = s.phones.filter((p) => p.online);
    if (online.length === 0) {
      blockers.push({
        code: 'no_phones',
        message: 'No phone online — open the app on at least one phone',
      });
    }
    if (!s.draft.subject.trim()) {
      blockers.push({ code: 'draft_subject', subject: 'subject', message: 'Subject is required' });
    }
    if (!s.draft.session_tag.trim()) {
      blockers.push({ code: 'draft_tag', subject: 'session_tag', message: 'Tag is required' });
    }
    if (!s.draft.operator.trim()) {
      blockers.push({ code: 'draft_operator', subject: 'operator', message: 'Operator is required' });
    }
    if (s.cameras.length === 0) {
      blockers.push({ code: 'no_cameras', message: 'Select at least one camera' });
    }
    for (const c of s.cameras) {
      if (c.state !== 'live') {
        blockers.push({
          code: 'camera_not_live',
          subject: c.cam_id,
          message: `${c.cam_id} is ${c.state} — check the USB connection`,
        });
      }
    }
    if (['RECORDING', 'FINALIZING', 'VALIDATING'].includes(s.state)) {
      blockers.push({ code: 'busy', message: 'A session is already recording or finalising' });
    }

    for (const p of online) {
      if (p.true_hz_avg > 0 && p.true_hz_avg < 90) {
        warnings.push({
          code: 'low_rate',
          subject: p.device_id,
          message: `${p.role} samples at ${p.true_hz_avg.toFixed(0)} Hz (< 90 Hz true rate)`,
        });
      }
    }
    const versions = [...new Set(online.map((p) => p.app_version))];
    if (versions.length > 1) {
      warnings.push({
        code: 'version_mismatch',
        message: `Phone app versions differ: ${versions.join(', ')}`,
      });
    }
    if (this.activeRefId) {
      const refName = this.references.find((r) => r.ref_id === this.activeRefId)?.name ?? this.activeRefId;
      for (const c of s.cameras) {
        const d = c.drift;
        if (d && d.verdict !== 'OK') {
          warnings.push({
            code: 'camera_drift',
            subject: c.cam_id,
            message: `${c.cam_id} ${d.verdict === 'MOVED' ? 'moved' : 'drifted'} ${Math.round(
              Math.hypot(d.dx_px, d.dy_px),
            )} px since reference '${refName}'`,
          });
        }
      }
    }
    return { blockers, warnings };
  }

  // -------------------------------------------------------------- scenarios

  private buildSnapshot(scenario: ScenarioId): Snapshot {
    const now = Date.now();
    const draftFilled: Draft = {
      subject: 'S012',
      session_tag: 'baseline',
      operator: 'francis',
      updated_at_ms: now,
    };
    const draftEmpty: Draft = { subject: '', session_tag: '', operator: '', updated_at_ms: now };

    const lowHz = scenario === 'idle';
    const phones = PHONE_BASE.map((p, i) =>
      makePhone(p.role, p.id, lowHz && i === 2 ? 72.1 : p.hz),
    );
    const cameras = [makeCam('cam1', 'fake:0', 'Fake Cam HD (front)'), makeCam('cam2', 'fake:1', 'Fake Cam HD (side)')];

    const base: Snapshot = {
      build_id: 'mock',
      server_time_ms: now,
      state: 'IDLE',
      session: null,
      last_session_id: null,
      draft: scenario === 'idle' ? draftEmpty : draftFilled,
      phones,
      cameras,
      readiness: { blockers: [], warnings: [] },
      finalize: null,
      notices: [],
      disk: { path: 'D:\\Riset\\Data_Riset_IMU', free_bytes: 42.3e9 },
      calibration: { active_ref_id: null },
    };

    switch (scenario) {
      case 'done-pass': {
        base.last_session_id = 'mock-0005';
        this.registerFinishedSession('mock-0005', base, now - 25 * 60_000, 'PASS', false);
        this.registerHistory(base);
        break;
      }
      case 'done-partial': {
        base.last_session_id = 'mock-0006';
        this.registerFinishedSession('mock-0006', base, now - 40 * 60_000, 'PARTIAL', true);
        this.registerHistory(base);
        const detail = this.sessions.get('mock-0006');
        base.notices = [
          {
            id: 'n-late-1',
            level: 'warn',
            code: 'late_data',
            message: 'waist: 1,204 late rows merged into the session CSV',
            session_id: 'mock-0006',
            created_at_ms: now - 38 * 60_000,
            resolved: false,
          },
          {
            id: 'n-upload-1',
            level: 'info',
            code: 'upload_outstanding',
            message: 'ankle_left rescue upload outstanding — keep the phone on Wi-Fi',
            session_id: 'mock-0006',
            created_at_ms: now - 37 * 60_000,
            resolved: false,
          },
        ];
        void detail;
        break;
      }
      case 'calibration': {
        const refA: Reference = this.makeReference('ref_20261028-091502_a3f1', 'lab-A 2026-10-28', now - 2 * 86_400_000, cameras);
        const refB: Reference = this.makeReference('ref_20261030-091502_b7c2', 'lab-A 2026-10-30', now - 3_600_000, cameras);
        this.references = [refB, refA];
        this.activeRefId = refB.ref_id;
        base.calibration.active_ref_id = refB.ref_id;
        const cam2 = base.cameras[1];
        if (cam2) cam2.drift = driftOf('MOVED', 14.2, -3.4, 0.31, 0.42);
        const cam1 = base.cameras[0];
        if (cam1) cam1.drift = driftOf('OK', 0.4, -0.2, 0.87, 0.81);
        break;
      }
      default:
        break;
    }
    return base;
  }

  private setupScenario(scenario: ScenarioId): void {
    const now = Date.now();
    switch (scenario) {
      case 'recording':
        this.startRecording({ startedAgoMs: 95_000 }, {
          labels: [
            { atMs: 4_000, id: 3 },
            { atMs: 9_000, id: 12 },
            { atMs: 16_000, id: 0 },
          ],
          dropPhoneAtMs: 12_000,
          dropPhoneRole: 'waist',
        });
        break;
      case 'finalizing': {
        this.startRecording({ startedAgoMs: 180_000 }, {});
        const sid = this.snapshot.session?.session_id;
        this.after(1_000, () => {
          if (sid) this.beginFinalize(sid);
        });
        break;
      }
      case 'camera-lost':
        this.startRecording({ startedAgoMs: 40_000 }, {
          labels: [{ atMs: 6_000, id: 5 }],
          loseCameraAtMs: 4_000,
          restoreCameraAtMs: 11_000,
          loseCamId: 'cam2',
        });
        break;
      default:
        void now;
        break;
    }
    // Phone telemetry ticks while the app lives: packets, ages and true Hz move.
    this.every(1_000, () => this.telemetryTick());
  }

  private telemetryTick(): void {
    let changed = false;
    for (const p of this.snapshot.phones) {
      if (p.online) {
        p.packets += Math.round(p.true_hz_avg);
        p.telemetry_age_ms = 30 + Math.round(Math.random() * 90);
        p.true_hz_avg = Math.max(1, p.true_hz_avg + (Math.random() - 0.5) * 0.4);
        changed = true;
      } else if (p.offline_since_ms !== null) {
        p.telemetry_age_ms = Date.now() - p.offline_since_ms;
      }
    }
    if (changed) this.update();
  }

  // ----------------------------------------------------------- session core

  private startRecording(opts: { startedAgoMs?: number }, script: RecordScript): void {
    this.sessionCounter += 1;
    const now = Date.now();
    const startedAt = now - (opts.startedAgoMs ?? 0);
    const sid = `mock-${String(now % 100_000)}-${this.sessionCounter}`;
    this.droppedPhones.clear();
    this.lostCameras.clear();
    this.snapshot.state = 'RECORDING';
    this.snapshot.session = {
      session_id: sid,
      subject: this.snapshot.draft.subject || 'S012',
      session_tag: this.snapshot.draft.session_tag || 'baseline',
      operator: this.snapshot.draft.operator || 'francis',
      started_at_ms: startedAt,
      scheduled_start_ms: startedAt,
      label: { id: 0, name: '0', since_ms: startedAt },
    };
    for (const c of this.snapshot.cameras) {
      c.recording = c.state === 'live';
    }
    this.startLive();
    this.update();

    for (const l of script.labels ?? []) {
      this.after(l.atMs, () => {
        if (this.snapshot.state === 'RECORDING' && this.snapshot.session) {
          this.snapshot.session.label = { id: l.id, name: String(l.id), since_ms: Date.now() };
          this.update();
        }
      });
    }
    if (script.dropPhoneAtMs && script.dropPhoneRole) {
      this.after(script.dropPhoneAtMs, () => {
        const phone = this.snapshot.phones.find((p) => p.role === script.dropPhoneRole);
        if (phone && this.snapshot.state === 'RECORDING') {
          phone.online = false;
          phone.offline_since_ms = Date.now();
          phone.buffering = true;
          this.droppedPhones.add(phone.device_id);
          this.update();
        }
      });
    }
    if (script.loseCameraAtMs && script.loseCamId) {
      const camId = script.loseCamId;
      this.after(script.loseCameraAtMs, () => {
        const cam = this.snapshot.cameras.find((c) => c.cam_id === camId);
        if (!cam || this.snapshot.state !== 'RECORDING') return;
        cam.state = 'lost';
        cam.recording = false;
        cam.fps = null;
        cam.error = 'capture process exited unexpectedly (exit code 1)';
        this.lostCameras.add(camId);
        this.snapshot.notices = [
          ...this.snapshot.notices,
          {
            id: `n-cam-${Date.now()}`,
            level: 'warn',
            code: 'camera_lost',
            message: `${camId} lost during recording — reconnecting, video resumes into _part2`,
            session_id: this.snapshot.session?.session_id ?? null,
            created_at_ms: Date.now(),
            resolved: false,
          },
        ];
        this.update();
      });
    }
    if (script.restoreCameraAtMs && script.loseCamId) {
      const camId = script.loseCamId;
      this.after(script.restoreCameraAtMs, () => {
        const cam = this.snapshot.cameras.find((c) => c.cam_id === camId);
        if (!cam || this.snapshot.state !== 'RECORDING') return;
        cam.state = 'live';
        cam.recording = true;
        cam.fps = 29.97;
        cam.error = null;
        // The notice auto-resolves when its condition clears (SPEC §6.2).
        this.snapshot.notices = this.snapshot.notices.filter(
          (n) => !(n.code === 'camera_lost' && n.message.startsWith(camId)),
        );
        this.update();
      });
    }
  }

  private startLive(): void {
    this.stopLive();
    const t0 = Date.now();
    this.liveTimer = this.every(100, () => {
      if (this.snapshot.state !== 'RECORDING') return;
      const t = (Date.now() - t0) / 1000;
      const phones: Record<string, { acc: [number, number, number]; gyro: [number, number, number]; ts_ms: number }> = {};
      this.snapshot.phones.forEach((p, idx) => {
        if (!p.online) return;
        const wob = Math.sin(t * 2 * Math.PI * (1.2 + idx * 0.35)) * 0.22;
        phones[p.device_id] = {
          acc: [
            0.08 * Math.sin(t * 9 + idx) + (Math.random() - 0.5) * 0.03,
            0.05 * Math.cos(t * 7 + idx) + (Math.random() - 0.5) * 0.03,
            1 + wob + (Math.random() - 0.5) * 0.04,
          ],
          gyro: [
            12 * Math.sin(t * 5 + idx) + (Math.random() - 0.5) * 4,
            9 * Math.cos(t * 4 + idx) + (Math.random() - 0.5) * 4,
            5 * Math.sin(t * 3) + (Math.random() - 0.5) * 3,
          ],
          ts_ms: Date.now(),
        };
      });
      this.emit({ type: 'live', data: { t_ms: Date.now(), phones } });
    });
  }

  private stopLive(): void {
    if (this.liveTimer !== null) {
      clearInterval(this.liveTimer);
      this.intervals.delete(this.liveTimer);
      this.liveTimer = null;
    }
  }

  private beginFinalize(sid: string): void {
    this.stopLive();
    this.snapshot.state = 'FINALIZING';
    for (const c of this.snapshot.cameras) c.recording = false;
    const steps: FinalizeStep[] = STEP_NAMES.map((name) => ({
      name,
      status: 'pending',
      started_at_ms: null,
      ended_at_ms: null,
      detail: null,
    }));
    const progress: FinalizeProgress = { session_id: sid, steps };
    this.snapshot.finalize = progress;
    this.update();

    // Script: each step runs ~1–2 s, then the session finishes.
    let t = 800;
    steps.forEach((_step, i) => {
      const start = t;
      const dur = 900 + i * 250;
      t = start + dur;
      this.after(start, () => {
        if (this.snapshot.finalize?.session_id !== sid) return;
        const s = this.snapshot.finalize.steps[i];
        if (!s) return;
        s.status = 'running';
        s.started_at_ms = Date.now();
        this.emit({ type: 'finalize', data: clone(this.snapshot.finalize as FinalizeProgress) });
        this.update();
      });
      this.after(t, () => {
        if (this.snapshot.finalize?.session_id !== sid) return;
        const s = this.snapshot.finalize.steps[i];
        if (!s) return;
        s.status = 'done';
        s.ended_at_ms = Date.now();
        if (s.name === 'package') s.detail = 'session ZIP written';
        if (s.name === 'consolidate') s.detail = 'every source is consolidated';
        this.emit({ type: 'finalize', data: clone(this.snapshot.finalize as FinalizeProgress) });
        this.update();
        if (i === steps.length - 1) this.finishSession(sid);
      });
    });
  }

  private finishSession(sid: string): void {
    const partial = this.droppedPhones.size > 0 || this.lostCameras.size > 0;
    this.registerFinishedSession(sid, this.snapshot, this.snapshot.session?.started_at_ms ?? Date.now(), partial ? 'PARTIAL' : 'PASS', partial);
    this.snapshot.state = 'IDLE';
    this.snapshot.session = null;
    this.snapshot.finalize = null;
    this.snapshot.last_session_id = sid;
    this.snapshot.draft.subject = ''; // Tag and Operator are kept; Subject is cleared
    if (partial && this.droppedPhones.size > 0) {
      const role = this.snapshot.phones.find((p) => this.droppedPhones.has(p.device_id))?.role ?? 'phone';
      this.snapshot.notices = [
        ...this.snapshot.notices,
        {
          id: `n-late-${sid}`,
          level: 'warn',
          code: 'late_data',
          message: `${role}: late rows merged into the session CSV (buffered while offline)`,
          session_id: sid,
          created_at_ms: Date.now(),
          resolved: false,
        },
      ];
    }
    this.update();
  }

  private registerFinishedSession(
    sid: string,
    snap: Snapshot,
    startedAt: number,
    verdict: 'PASS' | 'PARTIAL',
    withLate: boolean,
  ): void {
    const subject = snap.draft.subject || 'S012';
    const tag = snap.draft.session_tag || 'baseline';
    const endedAt = startedAt + 167_000;
    const folder = `${snap.disk.path}\\${subject}_${tag}\\${sid}_${subject}_${tag}`;
    const zipPath = `${folder}\\${subject}_${tag}_${sid}.zip`;
    const devices = snap.phones.map((p, i) => ({
      device_id: p.device_id,
      role: p.role,
      csv_path: `${folder}\\${sid}_${p.device_id}_imu.csv`,
      row_count: 16_700 - i * 37,
      csv_sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
      status: withLate && p.role === 'waist' ? 'PARTIAL' : 'PASS',
      ...(withLate && p.role === 'waist'
        ? { issue: 'offline 42 s during the session — 1 gap, late rows merged', offline_interval_count: 1, offline_total_ms: 42_000 }
        : { offline_interval_count: 0, offline_total_ms: 0 }),
    }));
    const late = withLate
      ? [{ device_id: 'R5CT7-02-waist', role: 'waist', rows: 1204 }]
      : [];
    const uploads: UploadStatus[] = withLate
      ? [
          {
            device_id: 'R5CT7-02-waist',
            role: 'waist',
            received_bytes: 1_284_000,
            total_bytes: 2_400_000,
            complete: false,
            verified: false,
          },
        ]
      : [];
    const files = [
      ...devices.map((d) => ({ name: `${sid}_${d.device_id}_imu.csv`, bytes: d.row_count * 96, kind: 'imu_csv' })),
      { name: `${sid}_cam1_video_sync.webm`, bytes: 184_000_000, kind: 'video' },
      { name: `${sid}_cam2_video_sync.webm`, bytes: 171_000_000, kind: 'video' },
      { name: 'cameras.json', bytes: 2_140, kind: 'json' },
      { name: 'video_report.json', bytes: 940, kind: 'json' },
    ];
    const detail: SessionDetail = {
      session_id: sid,
      subject,
      session_tag: tag,
      operator: snap.draft.operator || 'francis',
      started_at_ms: startedAt,
      ended_at_ms: endedAt,
      duration_s: (endedAt - startedAt) / 1000,
      phones: snap.phones.map((p) => p.device_id),
      cameras: ['cam1', 'cam2'],
      verdict,
      folder,
      zip_path: zipPath,
      zip_bytes: 348_900_000,
      outstanding_uploads: uploads.filter((u) => !u.complete).length,
      notices: withLate ? 2 : 0,
      finalize: STEP_NAMES.map((name) => ({
        name,
        status: 'done' as const,
        started_at_ms: endedAt,
        ended_at_ms: endedAt + 1_500,
        detail: null,
      })),
      integrity_report: {
        session_id: sid,
        status: verdict,
        validated_at_ms: endedAt + 4_000,
        devices,
        analysis_ready: true,
        analysis_ready_reasons: [],
      },
      files,
      late,
      uploads,
      consolidation: withLate
        ? {
            state: 'current',
            detail: 'every source is consolidated',
            can_consolidate: false,
            whole: false,
            late_pending: false,
            recovery_pending: false,
            uploads_in_progress: false,
            rescue_uploads: 0,
            late_rows: 1204,
          }
        : {
            state: 'not_needed',
            detail: 'session is whole',
            can_consolidate: false,
            whole: true,
            late_pending: false,
            recovery_pending: false,
            uploads_in_progress: false,
            rescue_uploads: 0,
            late_rows: 0,
          },
    };
    this.sessions.set(sid, detail);
  }

  private registerHistory(snap: Snapshot): void {
    const now = Date.now();
    this.registerFinishedSession('mock-hist-1', snap, now - 26 * 3_600_000, 'PASS', false);
    this.registerFinishedSession('mock-hist-2', snap, now - 50 * 3_600_000, 'PARTIAL', true);
  }

  private makeReference(refId: string, name: string, createdAt: number, cameras: Camera[]): Reference {
    const cams: ReferenceCamera[] = cameras.map((c) => ({
      device_key: c.device_key,
      label: c.label,
      cam_id_at_capture: c.cam_id,
      file: `${c.device_key.replace(/[^A-Za-z0-9._-]/g, '_')}.jpg`,
      width: c.width ?? 1280,
      height: c.height ?? 720,
      frame_arrival_ms: createdAt + 12,
      skew_ms: 12,
    }));
    const thumbUrls: Record<string, string> = {};
    for (const c of cameras) thumbUrls[c.device_key] = `mock://still/${refId}/${c.cam_id}`;
    return {
      ref_id: refId,
      name,
      created_at_ms: createdAt,
      trigger_ms: createdAt,
      cameras: cams,
      thumb_urls: thumbUrls,
    };
  }

  private compareCameras(): CompareCamera[] {
    return this.snapshot.cameras.map((c) => {
      const d = c.drift ?? driftOf('MISSING', 0, 0, 0, 0);
      const jitter = this.watchRefId ? (Math.random() - 0.5) * 0.6 : 0;
      return {
        verdict: d.verdict,
        dx_px: d.dx_px + jitter,
        dy_px: d.dy_px - jitter,
        shift_pct: d.shift_pct,
        score: d.score,
        confidence: d.confidence,
        at_ms: Date.now(),
        device_key: c.device_key,
        cam_id: c.cam_id,
      };
    });
  }

  private summarize(d: SessionDetail): SessionSummary {
    return {
      session_id: d.session_id,
      subject: d.subject,
      session_tag: d.session_tag,
      operator: d.operator,
      started_at_ms: d.started_at_ms,
      ended_at_ms: d.ended_at_ms,
      duration_s: d.duration_s,
      phones: d.phones,
      cameras: d.cameras,
      verdict: d.verdict,
      folder: d.folder,
      zip_path: d.zip_path,
      zip_bytes: d.zip_bytes,
      outstanding_uploads: d.outstanding_uploads,
      notices: d.notices,
    };
  }

  // ------------------------------------------------------------- Transport

  getState(): Promise<Snapshot> {
    return this.delay(() => clone(this.snapshot));
  }

  putDraft(update: DraftUpdate): Promise<Draft> {
    return this.delay(() => {
      const d = this.snapshot.draft;
      if (update.subject !== null && update.subject !== undefined) d.subject = update.subject;
      if (update.session_tag !== null && update.session_tag !== undefined) d.session_tag = update.session_tag;
      if (update.operator !== null && update.operator !== undefined) d.operator = update.operator;
      d.updated_at_ms = Date.now();
      this.update();
      return clone(d);
    });
  }

  startSession(): Promise<StartResponse> {
    return this.delay(() => {
      const { blockers, warnings } = this.computeReadiness();
      if (blockers.length > 0) {
        throw new ApiError(409, 'blocked', 'START refused — fix the blockers first', {
          blockers: clone(blockers),
        });
      }
      this.startRecording({}, { labels: [{ atMs: 6_000, id: 2 }] });
      const session = this.snapshot.session;
      if (!session) throw new ApiError(500, 'internal', 'start failed');
      return {
        session_id: session.session_id,
        scheduled_start_ms: session.scheduled_start_ms,
        warnings: clone(warnings),
      };
    });
  }

  stopSession(_reason?: string | null): Promise<StopResponse> {
    return this.delay(() => {
      const now = Date.now();
      // Idempotent for 10 s after a successful stop (API.md).
      if (this.lastStop && now - this.lastStop.atMs < 10_000 && this.snapshot.state !== 'RECORDING') {
        return clone(this.lastStop.body);
      }
      if (this.snapshot.state !== 'RECORDING' || !this.snapshot.session) {
        throw new ApiError(409, 'bad_state', 'Not recording — STOP refused');
      }
      const sid = this.snapshot.session.session_id;
      const body: StopResponse = { session_id: sid };
      this.lastStop = { atMs: now, body };
      this.beginFinalize(sid);
      return clone(body);
    });
  }

  setLabel(labelId: number): Promise<LabelResponse> {
    return this.delay(() => {
      if (this.snapshot.state !== 'RECORDING' || !this.snapshot.session) {
        throw new ApiError(409, 'bad_state', 'Labels can only be set while recording');
      }
      if (!Number.isInteger(labelId) || labelId < 0 || labelId > 50) {
        throw new ApiError(422, 'invalid', `label_id must be 0..50, got ${labelId}`);
      }
      const since = Date.now();
      this.snapshot.session.label = { id: labelId, name: String(labelId), since_ms: since };
      this.update();
      return { label_id: labelId, since_ms: since };
    });
  }

  listSessions(limit = 50, before?: string | null): Promise<SessionList> {
    return this.delay(() => {
      const all = [...this.sessions.values()]
        .map((d) => this.summarize(d))
        .sort((a, b) => b.started_at_ms - a.started_at_ms);
      let start = 0;
      if (before) {
        const idx = all.findIndex((s) => s.session_id === before);
        start = idx >= 0 ? idx + 1 : 0;
      }
      const page = all.slice(start, start + limit);
      const next = start + limit < all.length ? (page[page.length - 1]?.session_id ?? null) : null;
      return { sessions: page, next };
    });
  }

  getSession(sid: string): Promise<SessionDetail> {
    return this.delay(() => {
      const d = this.sessions.get(sid);
      if (!d) {
        // A session that is still finalising has no on-disk record yet; show
        // live progress instead of a hard 404 in the UI.
        const live = this.snapshot.session;
        if (live && live.session_id === sid && this.snapshot.finalize) {
          return {
            session_id: sid,
            subject: live.subject,
            session_tag: live.session_tag,
            operator: live.operator,
            started_at_ms: live.started_at_ms,
            ended_at_ms: null,
            duration_s: null,
            phones: this.snapshot.phones.map((p) => p.device_id),
            cameras: this.snapshot.cameras.map((c) => c.cam_id),
            verdict: 'NONE' as const,
            folder: '',
            zip_path: null,
            zip_bytes: null,
            outstanding_uploads: 0,
            notices: 0,
            finalize: clone(this.snapshot.finalize.steps),
            integrity_report: null,
            files: [],
            late: [],
            uploads: [],
            consolidation: null,
          };
        }
        throw new ApiError(404, 'not_found', `Session ${sid} not found`);
      }
      return clone(d);
    });
  }

  repackage(sid: string): Promise<PackageResponse> {
    return this.delay(() => {
      if (this.snapshot.state === 'FINALIZING' && this.snapshot.finalize?.session_id === sid) {
        throw new ApiError(409, 'busy', 'Packaging is already running');
      }
      const d = this.sessions.get(sid);
      return { zip_path: d?.zip_path ?? `${sid}.zip` };
    });
  }

  consolidate(sid: string): Promise<PackageResponse> {
    return this.delay(() => {
      if (this.snapshot.state === 'FINALIZING' && this.snapshot.finalize?.session_id === sid) {
        throw new ApiError(409, 'busy', 'The post-session job is already running');
      }
      if (this.snapshot.state === 'RECORDING' && this.snapshot.session?.session_id === sid) {
        throw new ApiError(409, 'bad_state', 'The session is still recording');
      }
      const d = this.sessions.get(sid);
      if (d?.consolidation) {
        // The backend's step merges every verified source, so the row turns current.
        d.consolidation = {
          ...d.consolidation,
          state: 'current',
          detail: 'every source is consolidated',
          can_consolidate: false,
          late_pending: false,
          recovery_pending: false,
          uploads_in_progress: false,
        };
        const step = d.finalize.find((s) => s.name === 'consolidate');
        if (step) {
          step.status = 'done';
          step.detail = 'every source is consolidated';
        }
      }
      return { zip_path: d?.zip_path ?? `${sid}.zip` };
    });
  }

  openFolder(_sid: string): Promise<void> {
    return this.delay(() => undefined);
  }

  dismissNotice(id: string): Promise<void> {
    return this.delay(() => {
      this.snapshot.notices = this.snapshot.notices.filter((n) => n.id !== id);
      this.update();
    });
  }

  resetPhones(): Promise<void> {
    return this.delay(() => {
      if (['RECORDING', 'FINALIZING', 'VALIDATING'].includes(this.snapshot.state)) {
        throw new ApiError(409, 'bad_state', 'Cannot reset phone connections while recording');
      }
      for (const p of this.snapshot.phones) {
        p.online = false;
        p.offline_since_ms = Date.now();
        p.telemetry_age_ms = null;
      }
      this.update();
      this.after(1_800, () => {
        for (const p of this.snapshot.phones) {
          p.online = true;
          p.offline_since_ms = null;
          p.telemetry_age_ms = 80;
        }
        this.update();
      });
    });
  }

  listCameraDevices(): Promise<DeviceList> {
    return this.delay(() => ({ devices: clone(this.devices) }));
  }

  putCameraSelection(cameras: CameraSelectionItem[]): Promise<CameraSelectionResponse> {
    return this.delay(() => {
      if (['RECORDING', 'FINALIZING', 'VALIDATING'].includes(this.snapshot.state)) {
        throw new ApiError(409, 'bad_state', 'Camera selection is locked while recording');
      }
      if (cameras.length > 5) throw new ApiError(422, 'invalid', 'At most 5 cameras');
      const next: Camera[] = cameras.map((sel, i) => {
        const dev = this.devices.find((d) => d.device_key === sel.device_key);
        const camId = sel.cam_id ?? `cam${i + 1}`;
        const prev = this.snapshot.cameras.find((c) => c.device_key === sel.device_key);
        if (prev) {
          prev.cam_id = camId;
          return prev;
        }
        return makeCam(camId, sel.device_key, dev?.label ?? sel.device_key);
      });
      // Newly added cameras open asynchronously.
      for (const c of next) {
        if (!this.snapshot.cameras.some((old) => old.device_key === c.device_key)) {
          c.state = 'opening';
          c.width = null;
          c.height = null;
          c.fps = null;
          this.after(900, () => {
            const live = this.snapshot.cameras.find((x) => x.device_key === c.device_key);
            if (live && live.state === 'opening') {
              live.state = 'live';
              live.width = 1280;
              live.height = 720;
              live.fps = 29.97;
              this.update();
            }
          });
        }
      }
      this.snapshot.cameras = next;
      this.update();
      return { cameras: clone(next) };
    });
  }

  createReference(body: ReferenceCreate = {}): Promise<Reference> {
    return this.delay(() => {
      const now = Date.now();
      const pad = (n: number) => String(n).padStart(2, '0');
      const d = new Date(now);
      const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
      const refId = `ref_${stamp}_m${String(this.sessionCounter).padStart(2, '0')}ck`;
      const live = this.snapshot.cameras.filter((c) => c.state === 'live');
      if (live.length === 0) throw new ApiError(409, 'busy', 'No live camera to capture');
      const ref = this.makeReference(
        refId,
        body.name ?? `Reference ${d.toLocaleString()}`,
        now,
        live,
      );
      this.references = [ref, ...this.references];
      return clone(ref);
    });
  }

  listReferences(): Promise<ReferenceList> {
    return this.delay(() => ({ references: clone(this.references), active_ref_id: this.activeRefId }));
  }

  renameReference(refId: string, name: string): Promise<Reference> {
    return this.delay(() => {
      const ref = this.references.find((r) => r.ref_id === refId);
      if (!ref) throw new ApiError(404, 'not_found', `Reference ${refId} not found`);
      ref.name = name;
      return clone(ref);
    });
  }

  activateReference(refId: string): Promise<void> {
    return this.delay(() => {
      const ref = this.references.find((r) => r.ref_id === refId);
      if (!ref) throw new ApiError(404, 'not_found', `Reference ${refId} not found`);
      this.activeRefId = refId;
      this.snapshot.calibration.active_ref_id = refId;
      if (this.watchRefId !== null) this.restartDrift();
      this.update();
    });
  }

  deactivateReferences(): Promise<void> {
    return this.delay(() => {
      this.activeRefId = null;
      this.snapshot.calibration.active_ref_id = null;
      for (const c of this.snapshot.cameras) c.drift = null;
      this.update();
    });
  }

  deleteReference(refId: string): Promise<void> {
    return this.delay(() => {
      if (refId === this.activeRefId) {
        throw new ApiError(409, 'invalid', 'The active reference cannot be deleted — deactivate it first');
      }
      const before = this.references.length;
      this.references = this.references.filter((r) => r.ref_id !== refId);
      if (this.references.length === before) throw new ApiError(404, 'not_found', `Reference ${refId} not found`);
    });
  }

  compare(refId = 'active'): Promise<CompareResult> {
    return this.delay(() => ({
      ref_id: refId === 'active' ? (this.activeRefId ?? 'active') : refId,
      at_ms: Date.now(),
      cameras: this.compareCameras(),
    }));
  }

  watchCalibration(refId: string): Promise<void> {
    return this.delay(() => {
      this.watchRefId = refId;
      this.restartDrift();
    });
  }

  unwatchCalibration(): Promise<void> {
    return this.delay(() => {
      this.watchRefId = null;
      if (this.driftTimer !== null) {
        clearInterval(this.driftTimer);
        this.intervals.delete(this.driftTimer);
        this.driftTimer = null;
      }
    });
  }

  private restartDrift(): void {
    if (this.driftTimer !== null) {
      clearInterval(this.driftTimer);
      this.intervals.delete(this.driftTimer);
      this.driftTimer = null;
    }
    if (this.watchRefId === null) return;
    const push = () => {
      if (this.watchRefId === null) return;
      this.emit({
        type: 'calibration.drift',
        data: {
          ref_id: this.watchRefId === 'active' ? (this.activeRefId ?? 'active') : this.watchRefId,
          at_ms: Date.now(),
          cameras: this.compareCameras(),
        },
      });
      // Keep the snapshot drift in sync so readiness warnings follow.
      for (const c of this.snapshot.cameras) {
        const cc = this.compareCameras().find((x) => x.device_key === c.device_key);
        if (cc && c.drift) {
          c.drift.dx_px = cc.dx_px;
          c.drift.dy_px = cc.dy_px;
          c.drift.at_ms = cc.at_ms;
        }
      }
    };
    push();
    this.driftTimer = this.every(500, push); // 2 Hz, like the real backend
  }

  // ------------------------------------------------------------------- URLs

  previewUrl(camId: string, w: number, fps: number): string {
    return `mock://preview/${camId}?w=${w}&fps=${fps}`;
  }

  zipUrl(sid: string): string {
    return `mock://zip/${sid}`;
  }

  edgesUrl(stillUrl: string): string {
    return `${stillUrl}?edges=1`;
  }

  // ------------------------------------------------------------- lifecycle

  subscribe(
    onMessage: (msg: ServerMessage) => void,
    onStatus?: (connected: boolean) => void,
  ): () => void {
    this.onMessage = onMessage;
    this.onStatus = onStatus ?? null;
    // Deliver the connect snapshot asynchronously, like a real socket would.
    Promise.resolve().then(() => {
      if (this.disposed) return;
      this.onStatus?.(true);
      this.emit({ type: 'snapshot', data: clone(this.snapshot) });
    });
    return () => this.dispose();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    for (const t of this.intervals) clearInterval(t);
    this.intervals.clear();
    this.liveTimer = null;
    this.driftTimer = null;
    this.onMessage = null;
    this.onStatus = null;
  }
}

/** Notice helper kept for typing clarity. */
export type MockNotice = Notice;
