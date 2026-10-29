import { ApiError } from './errors';
import type {
  CameraSelectionItem,
  CameraSelectionResponse,
  ClientMessage,
  CompareResult,
  DeviceList,
  Draft,
  DraftUpdate,
  LabelResponse,
  PackageResponse,
  Reference,
  ReferenceCreate,
  ReferenceList,
  ReferenceRename,
  ServerMessage,
  SessionDetail,
  SessionList,
  Snapshot,
  StartResponse,
  StopResponse,
} from './types';

/**
 * Everything the UI can do against the backend, as one interface with two
 * implementations: HttpTransport (fetch + WebSocket /ws/ui) and MockTransport
 * (in-memory, `npm run dev:mock`). Views never call fetch directly.
 */
export interface Transport {
  // --- REST commands (API.md) ---------------------------------------------
  getState(): Promise<Snapshot>;
  putDraft(update: DraftUpdate): Promise<Draft>;
  startSession(): Promise<StartResponse>;
  stopSession(reason?: string | null): Promise<StopResponse>;
  setLabel(labelId: number): Promise<LabelResponse>;
  listSessions(limit?: number, before?: string | null): Promise<SessionList>;
  getSession(sid: string): Promise<SessionDetail>;
  repackage(sid: string): Promise<PackageResponse>;
  openFolder(sid: string): Promise<void>;
  dismissNotice(id: string): Promise<void>;
  resetPhones(): Promise<void>;
  listCameraDevices(): Promise<DeviceList>;
  putCameraSelection(cameras: CameraSelectionItem[]): Promise<CameraSelectionResponse>;
  createReference(body?: ReferenceCreate): Promise<Reference>;
  listReferences(): Promise<ReferenceList>;
  renameReference(refId: string, name: string): Promise<Reference>;
  activateReference(refId: string): Promise<void>;
  deactivateReferences(): Promise<void>;
  deleteReference(refId: string): Promise<void>;
  compare(refId?: string): Promise<CompareResult>;

  // --- WebSocket-only commands ---------------------------------------------
  /** Send `calibration.watch`; resolves on the server `ack`. */
  watchCalibration(refId: string): Promise<void>;
  /** Send `calibration.unwatch`; resolves on the server `ack`. */
  unwatchCalibration(): Promise<void>;

  // --- URLs the UI puts straight into <img>/<a> -----------------------------
  previewUrl(camId: string, w: number, fps: number): string;
  zipUrl(sid: string): string;
  /** edges.png companion of a reference still URL (from `Reference.thumb_urls`). */
  edgesUrl(stillUrl: string): string;

  /**
   * Subscribe to server messages. `onStatus` reports WebSocket connectivity
   * (drives the "Reconnecting…" bar). Returns an unsubscribe function.
   */
  subscribe(
    onMessage: (msg: ServerMessage) => void,
    onStatus?: (connected: boolean) => void,
  ): () => void;

  /** Tear down sockets/timers (scenario switch in dev:mock, hot reload). */
  dispose(): void;
}

/** The subset of WebSocket HttpTransport uses, so tests can inject a fake. */
export interface WebSocketLike {
  readyState: number;
  onopen: ((ev?: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev?: unknown) => void) | null;
  onerror: ((ev?: unknown) => void) | null;
  send(data: string): void;
  close(): void;
}

export type WebSocketCtor = new (url: string) => WebSocketLike;

export interface HttpTransportOptions {
  /** Override for tests; defaults to the same-origin /ws/ui. */
  wsUrl?: string;
  WebSocketImpl?: WebSocketCtor;
  fetchImpl?: typeof fetch;
  /** Reconnect backoff starts here and doubles up to maxDelayMs (API.md). */
  baseDelayMs?: number;
  maxDelayMs?: number;
}

let reqCounter = 0;
function nextReqId(): string {
  reqCounter += 1;
  return `r${Date.now().toString(36)}-${reqCounter}`;
}

/**
 * Real backend transport: plain fetch for every command (so each one gets an
 * HTTP status) plus one WebSocket for snapshot/live/finalize/drift pushes.
 * On socket loss: "Reconnecting…", retry with backoff 0.5 s → 5 s, and on
 * reconnect simply take the fresh snapshot the server sends — there is no
 * reconciliation logic anywhere (API.md client rules).
 */
export class HttpTransport implements Transport {
  private readonly wsUrl: string;
  private readonly WebSocketImpl: WebSocketCtor;
  private readonly fetchImpl: typeof fetch;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;

  private ws: WebSocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private delayMs: number;
  private disposed = false;
  private subscribed = false;

  private onMessage: ((msg: ServerMessage) => void) | null = null;
  private onStatus: ((connected: boolean) => void) | null = null;
  private readonly pending = new Map<
    string,
    { resolve: () => void; reject: (e: ApiError) => void }
  >();

  constructor(opts: HttpTransportOptions = {}) {
    const proto = typeof location !== 'undefined' && location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = typeof location !== 'undefined' ? location.host : '127.0.0.1:8000';
    this.wsUrl = opts.wsUrl ?? `${proto}//${host}/ws/ui`;
    this.WebSocketImpl =
      opts.WebSocketImpl ??
      ((globalThis.WebSocket as unknown as WebSocketCtor) ??
        (() => {
          throw new Error('WebSocket unavailable');
        }));
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.baseDelayMs = opts.baseDelayMs ?? 500;
    this.maxDelayMs = opts.maxDelayMs ?? 5000;
    this.delayMs = this.baseDelayMs;
  }

  // --- REST -----------------------------------------------------------------

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const hasBody = body !== undefined;
    let res: Response;
    try {
      res = await this.fetchImpl(path, {
        method,
        headers: hasBody ? { 'content-type': 'application/json' } : undefined,
        body: hasBody ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      // Network-level failure (backend down, laptop asleep).
      throw new ApiError(0, 'network', e instanceof Error ? e.message : 'Network error');
    }
    if (!res.ok) throw await ApiError.fromResponse(res);
    const text = await res.text();
    return (text ? (JSON.parse(text) as T) : ({} as T));
  }

  getState(): Promise<Snapshot> {
    return this.request<Snapshot>('GET', '/api/state');
  }
  putDraft(update: DraftUpdate): Promise<Draft> {
    return this.request<Draft>('PUT', '/api/session/draft', update);
  }
  startSession(): Promise<StartResponse> {
    return this.request<StartResponse>('POST', '/api/session/start', {});
  }
  stopSession(reason?: string | null): Promise<StopResponse> {
    return this.request<StopResponse>('POST', '/api/session/stop', { reason: reason ?? null });
  }
  setLabel(labelId: number): Promise<LabelResponse> {
    return this.request<LabelResponse>('POST', '/api/session/label', { label_id: labelId });
  }
  listSessions(limit = 50, before?: string | null): Promise<SessionList> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (before) q.set('before', before);
    return this.request<SessionList>('GET', `/api/sessions?${q.toString()}`);
  }
  getSession(sid: string): Promise<SessionDetail> {
    return this.request<SessionDetail>('GET', `/api/sessions/${encodeURIComponent(sid)}`);
  }
  repackage(sid: string): Promise<PackageResponse> {
    return this.request<PackageResponse>('POST', `/api/sessions/${encodeURIComponent(sid)}/package`);
  }
  async openFolder(sid: string): Promise<void> {
    await this.request<unknown>('POST', `/api/sessions/${encodeURIComponent(sid)}/open-folder`);
  }
  async dismissNotice(id: string): Promise<void> {
    await this.request<unknown>('POST', `/api/notices/${encodeURIComponent(id)}/dismiss`);
  }
  async resetPhones(): Promise<void> {
    await this.request<unknown>('POST', '/api/phones/reset');
  }
  listCameraDevices(): Promise<DeviceList> {
    return this.request<DeviceList>('GET', '/api/cameras/devices');
  }
  putCameraSelection(cameras: CameraSelectionItem[]): Promise<CameraSelectionResponse> {
    return this.request<CameraSelectionResponse>('PUT', '/api/cameras/selection', { cameras });
  }
  createReference(body: ReferenceCreate = {}): Promise<Reference> {
    return this.request<Reference>('POST', '/api/calibration/references', body);
  }
  listReferences(): Promise<ReferenceList> {
    return this.request<ReferenceList>('GET', '/api/calibration/references');
  }
  renameReference(refId: string, name: string): Promise<Reference> {
    return this.request<Reference>(
      'PATCH',
      `/api/calibration/references/${encodeURIComponent(refId)}`,
      { name } satisfies ReferenceRename,
    );
  }
  async activateReference(refId: string): Promise<void> {
    await this.request<unknown>(
      'POST',
      `/api/calibration/references/${encodeURIComponent(refId)}/activate`,
    );
  }
  async deactivateReferences(): Promise<void> {
    await this.request<unknown>('POST', '/api/calibration/deactivate');
  }
  async deleteReference(refId: string): Promise<void> {
    await this.request<unknown>('DELETE', `/api/calibration/references/${encodeURIComponent(refId)}`);
  }
  compare(refId = 'active'): Promise<CompareResult> {
    return this.request<CompareResult>(
      'GET',
      `/api/calibration/compare?ref_id=${encodeURIComponent(refId)}`,
    );
  }

  // --- URLs ------------------------------------------------------------------

  previewUrl(camId: string, w: number, fps: number): string {
    return `/api/cameras/${encodeURIComponent(camId)}/preview.mjpg?w=${w}&fps=${fps}`;
  }
  zipUrl(sid: string): string {
    return `/api/sessions/${encodeURIComponent(sid)}/zip`;
  }
  edgesUrl(stillUrl: string): string {
    // Still URLs come from the server (`Reference.thumb_urls`, ending in
    // `<key_slug>.jpg`); CALIBRATION.md serves the edge PNG for the same slug at
    // `<ref_id>/<key_slug>/edges.png`. Deriving it from the server-provided URL
    // avoids re-implementing the slug hash on the client.
    return stillUrl.replace(/\.jpg(\?.*)?$/, '') + '/edges.png';
  }

  // --- WebSocket ---------------------------------------------------------------

  subscribe(
    onMessage: (msg: ServerMessage) => void,
    onStatus?: (connected: boolean) => void,
  ): () => void {
    this.onMessage = onMessage;
    this.onStatus = onStatus ?? null;
    this.subscribed = true;
    this.connect();
    return () => this.dispose();
  }

  private connect(): void {
    if (this.disposed || !this.subscribed) return;
    let ws: WebSocketLike;
    try {
      ws = new this.WebSocketImpl(this.wsUrl);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.delayMs = this.baseDelayMs; // backoff resets after a successful connect
      this.onStatus?.(true);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      let msg: unknown;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return; // garbage frame: ignore (forward compatibility)
      }
      if (typeof msg !== 'object' || msg === null) return;
      const m = msg as Record<string, unknown>;
      if (m['type'] === 'ack') {
        this.settleAck(m);
        return;
      }
      // Unknown message types are forwarded and ignored by the store.
      this.onMessage?.(msg as ServerMessage);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.rejectPending(new ApiError(0, 'disconnected', 'WebSocket closed'));
      this.onStatus?.(false);
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose follows; nothing to do here.
    };
  }

  private settleAck(m: Record<string, unknown>): void {
    const reqId = typeof m['req_id'] === 'string' ? m['req_id'] : null;
    if (!reqId) return;
    const p = this.pending.get(reqId);
    if (!p) return;
    this.pending.delete(reqId);
    if (m['ok'] === true) {
      p.resolve();
      return;
    }
    const err = (m['error'] ?? null) as { code?: string; message?: string; details?: unknown } | null;
    p.reject(
      new ApiError(
        400,
        err?.code ?? 'ack_failed',
        err?.message ?? 'Command failed',
        (err?.details as Record<string, unknown> | undefined) ?? null,
      ),
    );
  }

  private rejectPending(e: ApiError): void {
    for (const [, p] of this.pending) p.reject(e);
    this.pending.clear();
  }

  private scheduleReconnect(): void {
    if (this.disposed || !this.subscribed || this.reconnectTimer !== null) return;
    const delay = this.delayMs;
    this.delayMs = Math.min(this.delayMs * 2, this.maxDelayMs);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private send(msg: ClientMessage): Promise<void> {
    const ws = this.ws;
    if (!ws || ws.readyState !== 1) {
      return Promise.reject(
        new ApiError(0, 'disconnected', 'Not connected to the backend (reconnecting)'),
      );
    }
    const reqId = nextReqId();
    const payload = { ...msg, req_id: reqId } as ClientMessage;
    return new Promise<void>((resolve, reject) => {
      this.pending.set(reqId, { resolve, reject });
      try {
        ws.send(JSON.stringify(payload));
      } catch (e) {
        this.pending.delete(reqId);
        reject(new ApiError(0, 'send_failed', e instanceof Error ? e.message : 'send failed'));
      }
    });
  }

  watchCalibration(refId: string): Promise<void> {
    return this.send({ type: 'calibration.watch', ref_id: refId });
  }
  unwatchCalibration(): Promise<void> {
    return this.send({ type: 'calibration.unwatch' });
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.subscribed = false;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.rejectPending(new ApiError(0, 'disposed', 'Transport disposed'));
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null; // disposing must not trigger a reconnect
      try {
        ws.close();
      } catch {
        // already closed
      }
    }
    this.onMessage = null;
    this.onStatus = null;
  }
}
