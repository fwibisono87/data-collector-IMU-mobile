// WebSocket client for operator dashboard (CLAUDE.md §8).
// Connects to /ws/frontend (JSON commands) and /ws/live (sensor chart data).

export type SessionState =
  | "IDLE" | "PREFLIGHT" | "READY"
  | "RECORDING" | "FINALIZING" | "VALIDATING" | "ERROR";

export interface DeviceInfo {
  device_id: string;
  role: string;
  is_online: boolean;
  packets: number;
  substate?: string;
  first_packet_ts?: number;
  offline_intervals?: number;
  rate_hz?: number;
  true_hz?: number;
  true_hz_avg?: number;
  held_pct?: number;
  app_version?: string;
  device_model?: string;
  streaming?: boolean;
}

export interface StateUpdate {
  type: "STATE_UPDATE";
  state: SessionState;
  session_id: string;
  subject: string;
  session_tag: string;
  operator: string;
  devices: DeviceInfo[];
  quorum?: { connected: number; roles: string[] };
  scheduled_start_ms?: number;
  integrity_report?: Record<string, unknown>;
}

export interface AckMsg {
  type: "ACK";
  command_id: string;
  status: "ok" | "fail";
  detail?: string;
}

export interface FinalizeStep {
  step: string;
  state: "pending" | "running" | "done" | "failed" | "skipped";
  detail?: string;
  started_ms?: number;
  ended_ms?: number;
}

export interface FinalizeProgress {
  type: "FINALIZE_PROGRESS";
  session_id: string;
  started_ms: number;
  finished_ms: number;
  steps: FinalizeStep[];
  failed: string[];
}

export type FrontendMsg =
  | StateUpdate | AckMsg | FinalizeProgress
  | { type: string; [k: string]: unknown };

type Listener = (msg: FrontendMsg) => void;
type LiveListener = (samples: Record<string, { acc: number[]; gyro: number[]; ts: number }>) => void;

const ACK_TIMEOUT_MS = 2000;
const ACK_MAX_RETRIES = 3;
// STOP now only *starts* finalization on the backend and is acknowledged as soon as the
// session leaves RECORDING; the close/sort/validate/bundle work reports itself through
// FINALIZE_PROGRESS. The old 120 s timeout existed because the ACK was withheld for the
// whole job — and its retry then re-entered STOP and blanked the integrity report.
const STOP_ACK_TIMEOUT_MS = 15_000;

function newCommandId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

class WsClient {
  private controlWs: WebSocket | null = null;
  private liveWs: WebSocket | null = null;
  private listeners: Listener[] = [];
  private liveListeners: LiveListener[] = [];
  private connListeners: ((connected: boolean) => void)[] = [];
  private pendingAcks = new Map<string, {
    msg: string; attempts: number;
    resolve: (v: AckMsg) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout>;
  }>();
  private backendIp = "";
  private connectionGeneration = 0;

  connect(ip: string): void {
    this.backendIp = ip;
    const generation = ++this.connectionGeneration;
    this._connectControl(ip, generation);
    this._connectLive(ip, generation);
  }

  disconnect(): void {
    this.connectionGeneration++;
    this.controlWs?.close();
    this.liveWs?.close();
    this.controlWs = null;
    this.liveWs = null;
  }

  get isConnected(): boolean {
    return this.controlWs?.readyState === WebSocket.OPEN;
  }

  onMessage(cb: Listener): () => void {
    this.listeners.push(cb);
    return () => { this.listeners = this.listeners.filter(l => l !== cb); };
  }

  onLive(cb: LiveListener): () => void {
    this.liveListeners.push(cb);
    return () => { this.liveListeners = this.liveListeners.filter(l => l !== cb); };
  }

  onConnectionChange(cb: (connected: boolean) => void): () => void {
    this.connListeners.push(cb);
    return () => { this.connListeners = this.connListeners.filter(l => l !== cb); };
  }

  private _emitConn(connected: boolean): void {
    this.connListeners.forEach(l => l(connected));
  }

  /**
   * @param preflightFailed labels of preflight checks that were red at START. A failing check
   * warns rather than blocks, so the session must carry its own provenance: the backend
   * audit-logs these and stamps them into the CSV metadata line, making a substandard
   * recording self-documenting instead of indistinguishable from a clean one.
   */
  async startSession(
    subject: string, tag: string, operator: string, preflightFailed: string[] = [],
  ): Promise<AckMsg> {
    return this._sendWithAck("START_SESSION", {
      subject_name: subject, session_tag: tag, operator,
      preflight_failed: preflightFailed,
    });
  }

  async stopSession(reason = "operator_stop"): Promise<AckMsg> {
    return this._sendWithAck("STOP_SESSION", { reason }, undefined, 0, STOP_ACK_TIMEOUT_MS);
  }

  async setLabel(labelId: number): Promise<AckMsg> {
    return this._sendWithAck("SET_LABEL", { label_id: labelId, label_name: String(labelId) });
  }

  async resetConnections(): Promise<AckMsg> {
    return this._sendWithAck("RESET", {});
  }

  getState(): void {
    this._send("GET_STATE", {});
  }

  // ── Private ──────────────────────────────────────────────────────────────

  private _connectControl(ip: string, generation: number): void {
    const ws = new WebSocket(`ws://${ip}:8000/ws/frontend`);
    ws.onopen = () => {
      this._emitConn(true);
      this.getState();   // force a fresh snapshot after (re)connect / backend restart
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as FrontendMsg;
        if (msg.type === "ACK") this._resolveAck(msg as AckMsg);
        this.listeners.forEach(l => l(msg));
      } catch { /* ignore */ }
    };
    ws.onclose = () => {
      this._emitConn(false);
      this._rejectPending("Backend connection closed before the command was acknowledged.");
      if (generation === this.connectionGeneration) {
        setTimeout(() => {
          if (generation === this.connectionGeneration) this._connectControl(ip, generation);
        }, 3000);
      }
    };
    ws.onerror = () => ws.close();
    this.controlWs = ws;
  }

  private _connectLive(ip: string, generation: number): void {
    const ws = new WebSocket(`ws://${ip}:8000/ws/live`);
    ws.onmessage = (e) => {
      try {
        const { samples } = JSON.parse(e.data as string);
        this.liveListeners.forEach(l => l(samples));
      } catch { /* ignore */ }
    };
    ws.onclose = () => {
      if (generation === this.connectionGeneration) {
        setTimeout(() => {
          if (generation === this.connectionGeneration) this._connectLive(ip, generation);
        }, 3000);
      }
    };
    ws.onerror = () => ws.close();
    this.liveWs = ws;
  }

  private _send(type: string, payload: Record<string, unknown>, commandId?: string): string {
    const id = commandId ?? newCommandId();
    const msg = JSON.stringify({ type, payload, command_id: id });
    if (this.controlWs?.readyState === WebSocket.OPEN) {
      this.controlWs.send(msg);
    }
    return id;
  }

  private _sendWithAck(
    type: string,
    payload: Record<string, unknown>,
    commandId?: string,
    attempt = 0,
    timeoutMs = ACK_TIMEOUT_MS,
  ): Promise<AckMsg> {
    return new Promise((resolve, reject) => {
      const id = commandId ?? newCommandId();
      const msg = JSON.stringify({ type, payload, command_id: id });

      const timer = setTimeout(() => {
        this.pendingAcks.delete(id);
        if (attempt < ACK_MAX_RETRIES - 1) {
          this._sendWithAck(type, payload, id, attempt + 1, timeoutMs).then(resolve).catch(reject);
        } else {
          reject(new Error(`ACK timeout after ${ACK_MAX_RETRIES} attempts`));
        }
      }, timeoutMs);

      // Fail now rather than in six minutes. Previously the message was silently dropped
      // when the socket was mid-reconnect while the timeout stayed armed, so a STOP that
      // was never transmitted still took 3 x 120 s to report failure — with the cameras
      // recording throughout.
      if (this.controlWs?.readyState !== WebSocket.OPEN) {
        clearTimeout(timer);
        reject(new Error("Not connected to the backend — the command was not sent."));
        return;
      }

      this.pendingAcks.set(id, { msg, attempts: attempt, resolve, reject, timer });
      this.controlWs.send(msg);
    });
  }

  /** Fail every in-flight command when the socket drops, instead of leaving them to time out. */
  private _rejectPending(reason: string): void {
    const pending = Array.from(this.pendingAcks.values());
    this.pendingAcks.clear();
    for (const p of pending) {
      clearTimeout(p.timer);
      p.reject(new Error(reason));
    }
  }

  private _resolveAck(ack: AckMsg): void {
    const pending = this.pendingAcks.get(ack.command_id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingAcks.delete(ack.command_id);
    if (ack.status === "ok") pending.resolve(ack);
    else pending.reject(new Error(ack.detail ?? "ACK fail"));
  }
}

export const wsClient = new WsClient();
