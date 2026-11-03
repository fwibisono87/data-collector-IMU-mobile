import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import type { Transport } from '../api/transport';
import type {
  Camera,
  ConsolidationStatus,
  FinalizeStep,
  Phone,
  Reference,
  ServerMessage,
  SessionDetail,
  Snapshot,
} from '../api/types';
import { AppProvider, createApp, type App } from '../state/store';

/**
 * Shared test fixtures. Views are rendered around a real `createApp()` with a
 * stubbed Transport and a SEEDED ui store — the same wiring production uses,
 * minus the network.
 */

export function makePhone(over: Partial<Phone> = {}): Phone {
  return {
    device_id: 'dev-chest',
    role: 'chest',
    model: 'Galaxy A56',
    app_version: '3.0.0',
    online: true,
    offline_since_ms: null,
    rate_hz: 100,
    true_hz_avg: 100.2,
    packets: 1234,
    telemetry_age_ms: 50,
    buffering: false,
    ...over,
  };
}

export function makeCam(over: Partial<Camera> = {}): Camera {
  return {
    cam_id: 'cam1',
    device_key: 'fake:0',
    label: 'Fake Cam HD (front)',
    state: 'live',
    recording: false,
    width: 1280,
    height: 720,
    fps: 29.97,
    dropped_frames: 0,
    error: null,
    drift: null,
    ...over,
  };
}

export function makeSnapshot(over: Partial<Snapshot> = {}): Snapshot {
  const now = Date.now();
  return {
    build_id: 'test',
    server_time_ms: now,
    state: 'IDLE',
    session: null,
    last_session_id: null,
    draft: { subject: 'S012', session_tag: 'baseline', operator: 'francis', updated_at_ms: now },
    phones: [makePhone()],
    cameras: [makeCam()],
    readiness: { blockers: [], warnings: [] },
    finalize: null,
    notices: [],
    disk: { path: 'D:\\Riset\\Data_Riset_IMU', free_bytes: 42.3e9 },
    calibration: { active_ref_id: null },
    ...over,
  };
}

export function makeSession(over: Partial<Snapshot['session']> = {}): NonNullable<Snapshot['session']> {
  const now = Date.now();
  return {
    session_id: 's-test-1',
    subject: 'S012',
    session_tag: 'baseline',
    operator: 'francis',
    started_at_ms: now - 65_000,
    scheduled_start_ms: now - 65_000,
    label: { id: 0, name: '0', since_ms: now - 5_000 },
    ...over,
  };
}

export function makeStep(over: Partial<FinalizeStep> = {}): FinalizeStep {
  return {
    name: 'close_writers',
    status: 'done',
    started_at_ms: Date.now(),
    ended_at_ms: Date.now() + 100,
    detail: null,
    ...over,
  };
}

export function makeSessionDetail(over: Partial<SessionDetail> = {}): SessionDetail {
  const now = Date.now();
  return {
    session_id: 's-test-1',
    subject: 'S012',
    session_tag: 'baseline',
    operator: 'francis',
    started_at_ms: now - 200_000,
    ended_at_ms: now - 30_000,
    duration_s: 167,
    phones: ['dev-chest'],
    cameras: ['cam1'],
    verdict: 'PASS',
    folder: 'D:\\Riset\\Data_Riset_IMU\\S012_baseline\\s-test-1',
    zip_path: 'D:\\Riset\\Data_Riset_IMU\\S012_baseline\\s-test-1\\S012_baseline_s-test-1.zip',
    zip_bytes: 348_900_000,
    outstanding_uploads: 0,
    notices: 0,
    finalize: [makeStep()],
    integrity_report: {
      session_id: 's-test-1',
      status: 'PASS',
      devices: [
        { device_id: 'dev-chest', role: 'chest', status: 'PASS', row_count: 16_700 },
      ],
      analysis_ready: true,
    },
    files: [],
    late: [],
    uploads: [],
    ...over,
  };
}

export function makeConsolidation(over: Partial<ConsolidationStatus> = {}): ConsolidationStatus {
  return {
    state: 'pending_phone_data',
    detail: '1 rescue upload waiting to be merged',
    can_consolidate: true,
    whole: false,
    late_pending: false,
    recovery_pending: true,
    uploads_in_progress: false,
    rescue_uploads: 1,
    late_rows: 0,
    ...over,
  };
}

export function makeReference(over: Partial<Reference> = {}): Reference {
  const now = Date.now();
  return {
    ref_id: 'ref-test-1',
    name: 'lab-A test',
    created_at_ms: now - 3_600_000,
    trigger_ms: now - 3_600_000,
    cameras: [
      {
        device_key: 'fake:0',
        label: 'Fake Cam HD (front)',
        cam_id_at_capture: 'cam1',
        file: 'fake_0.jpg',
        width: 1280,
        height: 720,
        frame_arrival_ms: now - 3_600_000 + 12,
        skew_ms: 12,
      },
    ],
    thumb_urls: { 'fake:0': '/api/calibration/references/ref-test-1/fake_0.jpg' },
    ...over,
  };
}

/** A Transport whose every method is a vi.fn() with a resolving default. */
export type StubTransport = Transport & {
  /** Push a WS message into whatever subscribed (the app's dispatch). */
  emit(msg: ServerMessage): void;
  /** Flip the WS connectivity flag (drives the ReconnectBar). */
  setConnected(connected: boolean): void;
};

export function stubTransport(over: Partial<Transport> = {}): StubTransport {
  let onMessage: ((m: ServerMessage) => void) | null = null;
  let onStatus: ((c: boolean) => void) | null = null;
  const detail = makeSessionDetail();
  const stub = {
    getState: vi.fn(async () => makeSnapshot()),
    putDraft: vi.fn(async () => makeSnapshot().draft),
    startSession: vi.fn(async () => ({
      session_id: detail.session_id,
      scheduled_start_ms: Date.now(),
      warnings: [],
    })),
    stopSession: vi.fn(async () => ({ session_id: detail.session_id })),
    setLabel: vi.fn(async (labelId: number) => ({ label_id: labelId, since_ms: Date.now() })),
    listSessions: vi.fn(async () => ({
      sessions: [
        {
          session_id: detail.session_id,
          subject: detail.subject,
          session_tag: detail.session_tag,
          operator: detail.operator,
          started_at_ms: detail.started_at_ms,
          ended_at_ms: detail.ended_at_ms,
          duration_s: detail.duration_s,
          phones: detail.phones,
          cameras: detail.cameras,
          verdict: detail.verdict,
          folder: detail.folder,
          zip_path: detail.zip_path,
          zip_bytes: detail.zip_bytes,
          outstanding_uploads: 0,
          notices: 0,
        },
      ],
      next: null,
    })),
    getSession: vi.fn(async () => detail),
    consolidate: vi.fn(async () => ({ zip_path: detail.zip_path ?? 's.zip' })),
    repackage: vi.fn(async () => ({ zip_path: detail.zip_path ?? 's.zip' })),
    openFolder: vi.fn(async () => undefined),
    dismissNotice: vi.fn(async () => undefined),
    resetPhones: vi.fn(async () => undefined),
    listCameraDevices: vi.fn(async () => ({
      devices: [{ device_key: 'fake:0', label: 'Fake Cam HD (front)', backend: 'fake' }],
    })),
    putCameraSelection: vi.fn(async () => ({ cameras: [makeCam()] })),
    createReference: vi.fn(async () => makeReference()),
    listReferences: vi.fn(async () => ({ references: [makeReference()], active_ref_id: 'ref-test-1' })),
    renameReference: vi.fn(async () => makeReference()),
    activateReference: vi.fn(async () => undefined),
    deactivateReferences: vi.fn(async () => undefined),
    deleteReference: vi.fn(async () => undefined),
    compare: vi.fn(async () => ({ ref_id: 'ref-test-1', at_ms: Date.now(), cameras: [] })),
    watchCalibration: vi.fn(async () => undefined),
    unwatchCalibration: vi.fn(async () => undefined),
    previewUrl: vi.fn((camId: string, w: number, fps: number) => `/api/cameras/${camId}/preview.mjpg?w=${w}&fps=${fps}`),
    zipUrl: vi.fn((sid: string) => `/api/sessions/${sid}/zip`),
    edgesUrl: vi.fn((u: string) => u.replace(/\.jpg(\?.*)?$/, '.edges.png')),
    subscribe: vi.fn((m: (msg: ServerMessage) => void, s?: (c: boolean) => void) => {
      onMessage = m;
      onStatus = s ?? null;
      return () => {
        onMessage = null;
        onStatus = null;
      };
    }),
    dispose: vi.fn(),
    ...over,
    emit(msg: ServerMessage) {
      onMessage?.(msg);
    },
    setConnected(connected: boolean) {
      onStatus?.(connected);
    },
  };
  return stub as unknown as StubTransport;
}

export interface RenderWithAppResult extends RenderResult {
  app: App;
  transport: StubTransport;
}

/** Render `ui` inside the production provider stack with a seeded store. */
export function renderWithApp(
  ui: ReactElement,
  opts: {
    snapshot?: Snapshot | null;
    connected?: boolean;
    transport?: StubTransport;
    route?: string;
  } = {},
): RenderWithAppResult {
  const transport = opts.transport ?? stubTransport();
  const app = createApp(transport);
  app.ui.setState({
    connected: opts.connected ?? true,
    snapshot: opts.snapshot === undefined ? makeSnapshot() : opts.snapshot,
  });
  const result = render(
    <AppProvider value={app}>
      <MemoryRouter
        initialEntries={[opts.route ?? '/']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        {ui}
      </MemoryRouter>
    </AppProvider>,
  );
  return { ...result, app, transport };
}
