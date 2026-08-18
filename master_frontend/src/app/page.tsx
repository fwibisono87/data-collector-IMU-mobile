"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";

import {
  wsClient,
  type SessionState, type DeviceInfo, type StateUpdate, type FinalizeProgress,
} from "@/lib/ws_client";
import { armAudio } from "@/lib/alert_sound";
import StatusBanner from "@/components/StatusBanner";
import SessionForm from "@/components/SessionForm";
import PreflightPanel, { buildChecks } from "@/components/PreflightPanel";
import LabelingPanel from "@/components/LabelingPanel";
import IntegrityReport from "@/components/IntegrityReport";
import SafeBoundary from "@/components/SafeBoundary";
import DevicePanel from "@/components/DevicePanel";
import AlertCenter from "@/components/AlertCenter";
// Direct import — dynamic() breaks forwardRef so camRef.current would be null.
import MultiCameraRecorder, {
  type MultiCameraRecorderHandle,
  type CameraStatus,
} from "@/components/MultiCameraRecorder";
import AmbientBackdrop from "@/components/AmbientBackdrop";
import RecoveryModal from "@/components/RecoveryModal";
import VideoRecoveryModal from "@/components/VideoRecoveryModal";
import { clearChunks, isSessionSaved, listUnconfirmedSessions } from "@/lib/video_backup";
import EndSessionModal, {
  type EndSessionInfo,
  type EndSessionVideoResult,
} from "@/components/EndSessionModal";

// ECharts uses browser APIs — dynamic import keeps SSR safe.
const RealtimeChart = dynamic(() => import("@/components/RealtimeChart"), { ssr: false });

// ── Types ─────────────────────────────────────────────────────────────────────
type AppView = "connect" | "dashboard";
type Sample = { acc: number[]; gyro: number[]; ts: number };

const PENDING_END_KEY = "imu.pending-end-session.v1";
const ACKED_END_KEY = "imu.acked-end-session.v1";

function normalizeBackendHost(value: string): string {
  const host = value.trim();
  return host.toLowerCase() === "localhost" ? "127.0.0.1" : host;
}

function readPendingEnd(): EndSessionInfo | null {
  try {
    const raw = localStorage.getItem(PENDING_END_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as EndSessionInfo;
    return value?.sessionId ? value : null;
  } catch {
    return null;
  }
}

export default function Home() {
  // Connection
  const [view, setView] = useState<AppView>("connect");
  const [backendIp, setBackendIp] = useState("192.168.1.100");
  const [isWsConnected, setIsWsConnected] = useState(false);
  const [connectError, setConnectError] = useState("");

  // Session state (mirrored from backend)
  const [sessionState, setSessionState] = useState<SessionState>("IDLE");
  const [sessionId, setSessionId] = useState("");
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [quorum, setQuorum] = useState<{ connected: number; roles: string[] }>({ connected: 0, roles: [] });
  const [integrityReport, setIntegrityReport] = useState<Record<string, unknown> | null>(null);
  // Commit the backend reports for itself, for the preflight build-match check. Null until
  // /health has answered; the check reads that as "pending", never as a mismatch.
  const [backendBuildId, setBackendBuildId] = useState<string | null>(null);

  // Session form
  const [subject, setSubject] = useState("");
  const [sessionTag, setSessionTag] = useState("");
  const [operator, setOperator] = useState("");

  // Sensor chart
  const [liveSamples, setLiveSamples] = useState<Record<string, Sample>>({});

  // Labeling
  const [activeLabel, setActiveLabel] = useState(0);
  const [labelError, setLabelError] = useState("");

  // Cameras (1–5, dynamic)
  const [camStatus, setCamStatus] = useState<CameraStatus>({ ready: 0, total: 0, ok: false, restoring: false });
  const [cameraStartError, setCameraStartError] = useState("");
  const camRef = useRef<MultiCameraRecorderHandle>(null);

  // Guards a second STOP click from re-entering stop_recording ("Not recording" throw).
  const [isStopping, setIsStopping] = useState(false);

  // Reset-connections flow. Confirm modal is gated on !isRecording; a successful reset
  // remounts AlertCenter (clears its local connectivity events) and clears the report.
  const [showReset, setShowReset] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [alertResetKey, setAlertResetKey] = useState(0);
  const [resetError, setResetError] = useState("");
  const [showRecovery, setShowRecovery] = useState(false);
  const [showVideoRecovery, setShowVideoRecovery] = useState(false);
  // Sessions with footage still in IndexedDB that has never been confirmed written to disk.
  // Surfaced as a persistent banner: on 2026-08-07 footage sat here unreachable after a crash
  // and was one START press away from being reclaimed. [incident 2026-08-07]
  const [unconfirmed, setUnconfirmed] = useState<string[]>([]);

  // End-of-session export modal — replaces the immediate per-camera downloads.
  const [endSession, setEndSession] = useState<EndSessionInfo | null>(null);
  const [endVideoResults, setEndVideoResults] = useState<EndSessionVideoResult[]>([]);
  const [endMissed, setEndMissed] = useState<string[]>([]);
  const [endRecheckTick, setEndRecheckTick] = useState(0);
  // Per-step finalization progress from the backend. Before this existed the dashboard
  // held RECORDING for the whole close/sort/validate span with no way to tell a healthy
  // slow finalize from a hang.
  const [finalize, setFinalize] = useState<FinalizeProgress | null>(null);
  const [scheduledStartMs, setScheduledStartMs] = useState(0);
  const [cameraStartRetry, setCameraStartRetry] = useState(0);
  const endSessionOpenRef = useRef(false);
  const finalizationInFlightRef = useRef("");
  const lastSeenStateRef = useRef<SessionState>("IDLE");
  const cameraStartedSessionRef = useRef("");
  const cameraStartInFlightRef = useRef("");
  useEffect(() => { endSessionOpenRef.current = endSession !== null; }, [endSession]);

  const recoverTerminalSession = useCallback(async (ended: EndSessionInfo) => {
    if (!ended.sessionId || finalizationInFlightRef.current === ended.sessionId) return;
    if (endSessionOpenRef.current && endSession?.sessionId === ended.sessionId) return;
    finalizationInFlightRef.current = ended.sessionId;
    let results: EndSessionVideoResult[] = [];
    let missed: string[] = [];
    try {
      const out = await (camRef.current?.stopRecording() ?? Promise.resolve({ results: [], missed: [] }));
      results = out.results;
      missed = out.missed;
    } catch (e) {
      console.error("terminal camera finalisation failed", e);
      missed = ["camera finalisation failed — recover IndexedDB footage before a new session"];
    }
    setEndSession(ended);
    setEndVideoResults(results);
    setEndMissed(missed);
    setIsStopping(false);
    finalizationInFlightRef.current = "";
  }, [endSession]);

  const handleEndSessionClose = useCallback(() => {
    const sid = endSession?.sessionId;
    if (sid) {
      // Closing is only exposed after a successful ZIP write. Repeat the acknowledgement
      // here so a remounted/recovered modal cannot reopen the same terminal session after
      // the operator has already confirmed the export.
      localStorage.setItem(ACKED_END_KEY, sid);
      localStorage.removeItem(PENDING_END_KEY);
    }
    setEndSession(null);
    setEndVideoResults([]);
    setEndMissed([]);
    // The STOP response normally broadcasts IDLE, but a late modal/reconnect race can
    // leave the dashboard showing FINALIZING. Ask the backend for an authoritative snapshot
    // as the modal closes rather than leaving the operator on stale local state.
    if (wsClient.isConnected) wsClient.getState();
  }, [endSession]);

  // A terminal dialog is a recoverable workflow, not transient React state. Restore the
  // local pending session first; if the browser lost localStorage as well, ask the backend
  // ledger for the newest terminal record and reconstruct the same identity from disk.
  useEffect(() => {
    if (!isWsConnected || !backendIp || sessionState === "RECORDING" || endSession) return;
    let cancelled = false;
    const restore = async () => {
      const pending = readPendingEnd();
      let acked = localStorage.getItem(ACKED_END_KEY);

      // IndexedDB is the durable proof that the ZIP reached disk. LocalStorage can be
      // missing after an origin change (127.0.0.1 vs localhost) or a browser cleanup;
      // never reopen an already-exported session merely because that small acknowledgement
      // record disappeared.
      if (pending && pending.sessionId !== acked) {
        let belongsToLiveBackend = true;
        try {
          const healthResponse = await fetch(`http://${backendIp}:8000/health`);
          if (healthResponse.ok) {
            const health = await healthResponse.json() as { session_state?: string; session_id?: string };
            belongsToLiveBackend = String(health.session_id ?? "") === pending.sessionId ||
              String(health.session_state ?? "") === "RECORDING";
          }
        } catch {
          // Keep the pending session if the health probe is unavailable; the recovery
          // endpoint below remains the fallback for a temporarily disconnected backend.
        }
        if (!belongsToLiveBackend) {
          // A stale RECORDING ledger entry from an older backend/test process must not
          // strand the dashboard in an empty end-session dialog after a reload.
          localStorage.removeItem(PENDING_END_KEY);
        } else {
          const pendingSaved = await isSessionSaved(pending.sessionId).catch(() => false);
          if (pendingSaved) {
            acked = pending.sessionId;
            localStorage.setItem(ACKED_END_KEY, acked);
            localStorage.removeItem(PENDING_END_KEY);
          } else if (!cancelled) {
            setEndSession(pending);
            return;
          }
        }
      }

      try {
        const [response, healthResponse] = await Promise.all([
          fetch(`http://${backendIp}:8000/session/recovery`),
          fetch(`http://${backendIp}:8000/health`),
        ]);
        const data = response.ok
          ? await response.json() as { sessions?: Array<Record<string, unknown>> }
          : null;
        const health = healthResponse.ok
          ? await healthResponse.json() as { session_state?: string; session_id?: string }
          : null;
        const liveState = String(health?.session_state ?? "");
        const liveSessionId = String(health?.session_id ?? "");
        if (cancelled || !data?.sessions?.length) return;
        const terminal = data.sessions.find(s => {
          const state = String(s.state ?? "");
          const activeRecording = state === "RECORDING" && liveState === "RECORDING" &&
            String(s.session_id ?? "") === liveSessionId;
          return (s.terminal === true || s.startup_in_progress === true || activeRecording ||
            ["FINALIZING", "VALIDATING", "ERROR"].includes(state)) &&
            String(s.session_id ?? "") !== acked;
        });
        if (!terminal) return;
        const restored: EndSessionInfo = {
          sessionId: String(terminal.session_id),
          subject: String(terminal.subject_name ?? ""),
          sessionTag: String(terminal.session_tag ?? ""),
          operator: String(terminal.operator ?? ""),
        };
        const restoredSaved = await isSessionSaved(restored.sessionId).catch(() => false);
        if (restoredSaved) {
          localStorage.setItem(ACKED_END_KEY, restored.sessionId);
          localStorage.removeItem(PENDING_END_KEY);
          return;
        }
        localStorage.setItem(PENDING_END_KEY, JSON.stringify(restored));
        setEndSession(restored);
      } catch {
        // The live dashboard remains usable if the recovery endpoint is temporarily down.
      }
    };
    void restore();
    return () => { cancelled = true; };
  }, [backendIp, endSession, isWsConnected, sessionState]);

  const isRecording = sessionState === "RECORDING";
  const supportsDurableVideoExport = typeof window !== "undefined" && "showSaveFilePicker" in window;
  // Derive online count directly from devices — single source of truth.
  const onlineCount = devices.filter(d => d.is_online).length;
  // Single source of truth for preflight, shared with PreflightPanel so the panel and the
  // START button can never disagree again. Previously this file hand-rolled its own boolean
  // that omitted "Sampling rate healthy", so the panel could read ✗ NO-GO for a 50 Hz device
  // while START stayed enabled.
  const preflightChecks = buildChecks(
    isWsConnected, devices, subject, sessionTag, operator, camStatus, backendBuildId,
  );
  const preflightFailures = preflightChecks.filter(c => c.status === "fail");
  const hasBuildMismatch = preflightFailures.some(c => c.label === "Dashboard build matches backend");

  // Hard prerequisites: without these a session cannot physically start (no backend, no
  // device, no session identity, no camera). Deliberately a SUBSET of the checks above —
  // quality problems such as a half-rate sensor warn loudly but do not block, so a field
  // session is never stranded by a judgement call. The difference is intentional and named,
  // rather than an accident of two divergent expressions.
  const canStart =
    isWsConnected &&
    onlineCount > 0 &&
    subject.trim().length > 0 &&
    sessionTag.trim().length > 0 &&
    operator.trim().length > 0 &&
    camStatus.ok &&
    !camStatus.restoring &&
    supportsDurableVideoExport &&
    !hasBuildMismatch;

  // Ask the backend which commit it is, once we're connected. A backend cannot change build
  // without restarting, which drops the socket — so reconnecting re-runs this and the answer
  // can never go quietly stale.
  useEffect(() => {
    if (!isWsConnected || !backendIp) return;
    let cancelled = false;
    probeBackend(backendIp).then(probe => {
      if (!cancelled && probe.ok) setBackendBuildId(probe.buildId ?? "unknown");
    });
    return () => { cancelled = true; };
  }, [isWsConnected, backendIp]);

  // ── WS event subscriptions ─────────────────────────────────────────────────
  useEffect(() => {
    const unsub = wsClient.onMessage((msg) => {
      if (msg.type === "STATE_UPDATE") {
        const su = msg as StateUpdate & {
          quorum?: { connected: number; roles: string[] };
          scheduled_start_ms?: number;
        };
        const previousState = lastSeenStateRef.current;
        lastSeenStateRef.current = su.state;
        setSessionState(su.state);
        setSessionId(su.session_id ?? "");
        setScheduledStartMs(su.scheduled_start_ms ?? 0);
        // STATE_UPDATE always carries the authoritative, complete device list. Apply it
        // verbatim — including an empty list — so pruned/offline devices and a backend
        // restart clear stale cards instead of lingering until a manual reload.
        if (su.devices) setDevices(su.devices);
        if (su.quorum) setQuorum(su.quorum);
        // Guard against an empty report overwriting a real one. The backend no longer
        // sends `{}`, but a mixed-version pair must not lose the operator's verdict.
        if (su.integrity_report && Object.keys(su.integrity_report).length > 0) {
          setIntegrityReport(su.integrity_report);
        }
        const snapshot = (msg as { finalize?: FinalizeProgress | null }).finalize;
        if (snapshot) setFinalize(snapshot);

        if (su.state === "RECORDING" && su.session_id) {
          const active: EndSessionInfo = {
            sessionId: su.session_id,
            subject: su.subject ?? subject,
            sessionTag: su.session_tag ?? sessionTag,
            operator: su.operator ?? operator,
          };
          localStorage.setItem(PENDING_END_KEY, JSON.stringify(active));
        }

        if (
          su.state === "IDLE" && su.session_id &&
          (previousState === "RECORDING" || previousState === "FINALIZING" ||
            previousState === "VALIDATING" || readPendingEnd()?.sessionId === su.session_id)
        ) {
          const ended: EndSessionInfo = {
            sessionId: su.session_id,
            subject: su.subject ?? subject,
            sessionTag: su.session_tag ?? sessionTag,
            operator: su.operator ?? operator,
          };
          // STOP normally produces a late IDLE update after the export modal has already
          // closed. Do not interpret that acknowledgement as a second terminal session;
          // the old path reopened the modal and made the operator download the same ZIP
          // again. The IndexedDB marker covers reload/origin-change cases where the local
          // storage acknowledgement is absent.
          const acknowledged = localStorage.getItem(ACKED_END_KEY) === su.session_id;
          if (!acknowledged) {
            void isSessionSaved(su.session_id).then(saved => {
              if (saved) {
                localStorage.setItem(ACKED_END_KEY, su.session_id);
                localStorage.removeItem(PENDING_END_KEY);
                return;
              }
              void recoverTerminalSession(ended);
            }).catch(() => {
              void recoverTerminalSession(ended);
            });
          }
        }

      } else if (msg.type === "FINALIZE_PROGRESS") {
        setFinalize(msg as unknown as FinalizeProgress);
      } else if (msg.type === "LATE_DELIVERY") {
        // A phone flushed its buffered tail after STOP, into a *_late.csv sidecar
        // (plan DD-4). If the export modal is open it re-checks itself; otherwise the
        // operator must know the sidecar exists and needs merging before analysis.
        const s = msg as unknown as { session_id: string; devices: Record<string, { rows_appended: number }> };
        const total = Object.values(s.devices ?? {}).reduce((a, d) => a + (d.rows_appended ?? 0), 0);
        if (endSessionOpenRef.current) {
          setEndRecheckTick(t => t + 1);
        } else {
          alert(
            `Late delivery received for session ${s.session_id}: ${total.toLocaleString()} rows ` +
            `written to *_sensor_data_late.csv. Merge with the main CSV before analysis.`
          );
        }
      }
    });
    const unsubLive = wsClient.onLive((samples) => setLiveSamples({ ...samples }));
    const unsubConn = wsClient.onConnectionChange(setIsWsConnected);

    return () => { unsub(); unsubLive(); unsubConn(); };
  }, [operator, recoverTerminalSession, sessionTag, subject]);

  // Camera acquisition is asynchronous. On a reload during RECORDING, the authoritative
  // state can arrive before getUserMedia has recreated the tiles; a one-shot timer at that
  // point used to silently produce a session with no video. Wait for all selected cameras to
  // be ready, then retry a failed start until the session ends.
  useEffect(() => {
    if (
      sessionState !== "RECORDING" || !sessionId || !scheduledStartMs || !camStatus.ok || camStatus.restoring ||
      cameraStartedSessionRef.current === sessionId || cameraStartInFlightRef.current === sessionId
    ) return;
    const delay = Math.max(0, scheduledStartMs - Date.now());
    const timer = window.setTimeout(() => {
      const recorder = camRef.current;
      if (!recorder) {
        setCameraStartRetry(n => n + 1);
        return;
      }
      cameraStartInFlightRef.current = sessionId;
      recorder.startRecording(sessionId)
        .then(() => {
          cameraStartedSessionRef.current = sessionId;
          setCameraStartError("");
        })
        .catch(error => {
          console.error("camera recording start failed", error);
          setCameraStartError(`Camera recording could not start: ${error}`);
          setCameraStartRetry(n => n + 1);
        })
        .finally(() => { cameraStartInFlightRef.current = ""; });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [camStatus.ok, camStatus.restoring, cameraStartRetry, scheduledStartMs, sessionId, sessionState]);

  // ── Auto-reconnect on mount ────────────────────────────────────────────────
  useEffect(() => {
    const saved = localStorage.getItem("backendIp");
    if (!saved) return;

    const host = normalizeBackendHost(saved);
    setBackendIp(host);
    wsClient.connect(host);

    let tries = 0;
    const poll = setInterval(() => {
      if (wsClient.isConnected) {
        clearInterval(poll);
        setIsWsConnected(true);
        setView("dashboard");
        wsClient.getState();
      } else if (++tries > 25) {
        clearInterval(poll);
        // Backend unreachable — stay on connect screen with IP pre-filled.
      }
    }, 200);

    return () => clearInterval(poll);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Unsaved-footage watch ──────────────────────────────────────────────────
  // Re-checked whenever the recovery screen closes, a session ends, or the session state
  // changes, so the banner clears as soon as footage is confirmed saved.
  useEffect(() => {
    let cancelled = false;
    listUnconfirmedSessions()
      .then(ids => { if (!cancelled) setUnconfirmed(ids); })
      .catch(() => { /* IndexedDB unavailable — banner simply stays empty */ });
    return () => { cancelled = true; };
  }, [showVideoRecovery, endSession, sessionState]);

  // ── Connect ────────────────────────────────────────────────────────────────
  const handleConnect = () => {
    armAudio();   // the click is the required user gesture before browser audio can play
    setConnectError("");
    const host = normalizeBackendHost(backendIp);
    setBackendIp(host);
    wsClient.connect(host);

    // Poll until WS opens (max 5s).
    let tries = 0;
    const poll = setInterval(() => {
      if (wsClient.isConnected) {
        clearInterval(poll);
        localStorage.setItem("backendIp", host);
        setIsWsConnected(true);
        setView("dashboard");
        wsClient.getState();
      } else if (++tries > 25) {
        clearInterval(poll);
        probeBackend(host).then(probe => {
          if (probe.ok) {
            const ipHint = probe.lanIp && probe.lanIp !== backendIp
              ? ` Backend reports its IP is ${probe.lanIp} — try that.`
              : "";
            setConnectError(
              `HTTP reachable but WebSocket did not open — check that this is the backend, not another service on :8000.${ipHint}`
            );
          } else {
            setConnectError(
              `${probe.reason}\nCheck: backend running? (run ops\\ip.ps1) · same Wi-Fi/subnet? · IP correct? · firewall allows inbound TCP 8000?`
            );
          }
        });
      }
    }, 200);
  };

  // ── Session controls ───────────────────────────────────────────────────────
  const handleStart = async () => {
    setIntegrityReport(null);
    setActiveLabel(0);
    setLabelError("");
    setCameraStartError("");
    try {
      await wsClient.startSession(
        subject, sessionTag, operator,
        preflightFailures.map(c => (c.detail ? `${c.label}: ${c.detail}` : c.label)),
      );
      // Webcam start is triggered by STATE_UPDATE with scheduled_start_ms
      // for coordinated sync with mobile devices (CLAUDE.md §22.5)
    } catch (e) {
      alert(`Start failed: ${e}`);
    }
  };

  const handleStop = async () => {
    if (isStopping) return;          // guard double-click → no spurious "Not recording" alert
    setIsStopping(true);
    setFinalize(null);
    // Capture identity BEFORE the stop call — a late STATE_UPDATE broadcast after the ACK
    // could otherwise describe the session differently than the one we just ended.
    const ended = { sessionId, subject, sessionTag, operator };

    // Tell the backend and finalize the cameras concurrently. Camera finalisation used to
    // sit behind the backend round trip, so an unreachable backend kept every camera
    // recording for as long as STOP took to fail. Neither depends on the other: the
    // backend owns the CSVs, the browser owns the footage.
    const backendStop = wsClient
      .stopSession("operator_stop")
      .then(() => "")
      .catch((e) => {
        console.error("session stop on backend failed", e);
        return String(e instanceof Error ? e.message : e);
      });
    // The state-update path normally opens this modal. Calling the same idempotent
    // recovery helper here covers a lost ACK/broadcast and guarantees the manual Stop
    // path cannot strand camera finalisation in a separate code path.
    const [stopError] = await Promise.all([backendStop, recoverTerminalSession(ended)]);
    if (stopError) {
      alert(
        `The backend did not confirm the stop: ${stopError}\n\n` +
        "Your footage has been finalized locally. Check the end-of-session dialog — " +
        "if the recording reached the backend it can still be downloaded from there.",
      );
    }
  };

  const handleLabel = async (id: number) => {
    setLabelError("");
    try {
      await wsClient.setLabel(id);
      setActiveLabel(id);
    } catch {
      setLabelError(`Label ${id} failed — retried 3×`);
    }
  };

  const handleReset = async () => {
    setResetError("");
    setIsResetting(true);
    try {
      await wsClient.resetConnections();
      setIntegrityReport(null);
      setLiveSamples({});
      setAlertResetKey(k => k + 1);   // remount AlertCenter → clears its event log
      setShowReset(false);
    } catch (e) {
      setResetError(`Reset failed: ${e}`);
    } finally {
      setIsResetting(false);
    }
  };

  // ── Render: connect screen ─────────────────────────────────────────────────
  if (view === "connect") {
    return (
      <>
        <AmbientBackdrop state="IDLE" />
        <div className="relative z-10 min-h-screen flex items-center justify-center">
          <div className="w-full max-w-sm space-y-4 p-8 glass-panel">
            <h1 className="text-xl font-bold text-center">IMU Telemetry</h1>
            <p className="text-xs text-gray-500 text-center">Operator Dashboard</p>
            <div>
              <label className="text-xs text-gray-400">Backend IP</label>
              <input
                className="glass-input w-full mt-1 px-3 py-2 text-sm"
                value={backendIp}
                onChange={e => setBackendIp(e.target.value)}
                placeholder="192.168.1.100"
              />
            </div>
            {connectError && <p className="text-xs text-red-400 whitespace-pre-line">{connectError}</p>}
            <button
              onClick={handleConnect}
              className="btn-primary w-full py-2 font-bold text-sm"
            >
              Connect
            </button>
            {isWsConnected && (
              <button
                onClick={() => setView("dashboard")}
                className="btn-glass w-full py-2 text-sm text-gray-300"
              >
                ← Back to Dashboard
              </button>
            )}
          </div>
        </div>
      </>
    );
  }

  // ── Render: dashboard ──────────────────────────────────────────────────────
  return (
    <>
      <AmbientBackdrop state={sessionState} />
      <div className="relative z-10 flex flex-col h-screen overflow-hidden">
        <StatusBanner state={sessionState} sessionId={sessionId} devices={devices} isWsConnected={isWsConnected} backendIp={backendIp} />

        {/* Unsaved footage is retained across sessions now, but it is only retained — it still
            needs saving. Persistent and unmissable, because the 2026-08-07 footage sat
            invisible in IndexedDB while the operator believed it was gone. */}
        {unconfirmed.length > 0 && (
          <div className="shrink-0 bg-amber-600/20 border-y border-amber-500/50 px-4 py-2 text-sm
                          text-amber-100 flex items-center gap-3">
            <span>⚠</span>
            <span className="flex-1">
              Video from {unconfirmed.length} session{unconfirmed.length > 1 ? "s" : ""} is still
              held in this browser and has never been confirmed saved to disk
              {" "}({unconfirmed.join(", ")}). It is kept until you save it.
            </span>
            <button
              onClick={() => setShowVideoRecovery(true)}
              className="btn-glass px-3 py-1 text-xs text-amber-200 whitespace-nowrap"
            >
              Save it now
            </button>
          </div>
        )}

        {isRecording && devices.some(d => !d.is_online) && (
          <div className="shrink-0 bg-red-600/25 border-y border-red-500/50 px-4 py-2 text-sm
                          text-red-200 font-bold flex items-center gap-3 animate-pulse">
            <span>⚠</span>
            <span>
              {devices.filter(d => !d.is_online).map(d => d.role).join(", ")} OFFLINE — data is
              buffering on the phone and will be re-sent on reconnect. Do not stop the session yet.
            </span>
          </div>
        )}

        <div className="flex flex-1 gap-0 overflow-hidden min-h-0">
          {/* Left panel */}
          <aside className="glass-rail w-64 shrink-0 border-r border-white/10 flex flex-col gap-4 p-4 overflow-y-auto">
            <SessionForm
              subject={subject} setSubject={setSubject}
              sessionTag={sessionTag} setSessionTag={setSessionTag}
              operator={operator} setOperator={setOperator}
              disabled={isRecording}
            />
            <DevicePanel
              devices={devices}
              quorum={quorum}
              liveSamples={liveSamples}
              isRecording={isRecording}
            />
            <div className="glass-panel p-3">
              <AlertCenter key={alertResetKey} devices={devices} state={sessionState} isWsConnected={isWsConnected} />
            </div>
            <PreflightPanel checks={preflightChecks} devices={devices} />

            {/* Open the recover-from-devices modal */}
            <button
              onClick={() => setShowRecovery(true)}
              className="btn-glass w-full py-2 text-xs text-gray-300"
            >
              Download / merge phone rescue files
            </button>

            {/* Buffered webcam footage held in this browser. Reachable even after a reload —
                the failure on 2026-08-07 was that nothing in the UI could get at it. */}
            <button
              onClick={() => setShowVideoRecovery(true)}
              className={`btn-glass w-full py-2 text-xs ${
                unconfirmed.length > 0 ? "text-amber-300 border-amber-500/40" : "text-gray-300"
              }`}
            >
              Recover buffered video
              {unconfirmed.length > 0 && ` (${unconfirmed.length} unsaved)`}
            </button>

            {/* A failing quality check does not disable START — a field session must never be
                stranded by a judgement call — but it must be impossible to start one believing
                the setup is clean. The banner is not dismissible while the failure holds, and
                the failures are sent with START_SESSION so the recording documents itself. */}
            {!isRecording && preflightFailures.length > 0 && (
              <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs">
                <div className="font-bold text-red-300 mb-1">⚠ Preflight failing — you can still record</div>
                <ul className="list-disc list-inside text-red-200/90 space-y-0.5">
                  {preflightFailures.map(c => (
                    <li key={c.label}>
                      {c.label}
                      {c.detail ? <span className="text-red-300/80"> — {c.detail}</span> : null}
                    </li>
                  ))}
                </ul>
                {/* A build mismatch has exactly one fix, so offer it rather than describing it.
                    reload(true) is non-standard and ignored by modern browsers; the document is
                    already no-store (see layout.tsx force-dynamic), so a plain reload is enough
                    to pick up the current bundle. */}
                {preflightFailures.some(c => c.label === "Dashboard build matches backend") && (
                  <button
                    onClick={() => window.location.reload()}
                    className="mt-2 btn-glass text-xs text-cyan-300 border-cyan-500/40 px-2 py-1"
                  >
                    ⟳ Reload dashboard
                  </button>
                )}
              </div>
            )}

            {/* Start / Stop button */}
            {!isRecording ? (
              <button
                onClick={handleStart}
                disabled={!canStart}
                className={`w-full py-2 font-bold text-sm disabled:opacity-30 disabled:cursor-not-allowed ${
                  preflightFailures.length > 0 ? "btn-glass border-red-500/50 text-red-200" : "btn-success"
                }`}
              >
                {preflightFailures.length > 0 ? "▶ START ANYWAY (preflight failing)" : "▶ START SESSION"}
              </button>
            ) : (
              <button
                onClick={handleStop}
                disabled={isStopping}
                className="btn-danger w-full py-2 font-bold text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isStopping ? "■ STOPPING…" : "■ STOP SESSION"}
              </button>
            )}

            {/* Disconnect — locked during RECORDING so the camera MediaRecorders are never
                torn down mid-capture (which would silently drop video chunks). */}
            <button
              onClick={() => { wsClient.disconnect(); setView("connect"); setIsWsConnected(false); }}
              disabled={isRecording}
              title={isRecording ? "Stop the session before disconnecting" : undefined}
              className="text-xs text-gray-600 hover:text-gray-400 underline text-center disabled:opacity-30 disabled:cursor-not-allowed disabled:no-underline"
            >
              Disconnect
            </button>

            {/* Reset connections — clears devices + connectivity history. Locked during
                RECORDING; confirm-gated so it can't be triggered accidentally. */}
            <button
              onClick={() => { setResetError(""); setShowReset(true); }}
              disabled={isRecording || isResetting}
              title={isRecording ? "Stop the session first" : "Close all device connections and clear connectivity warnings (does NOT touch recovery files)"}
              className="text-xs text-red-500/70 hover:text-red-400 underline text-center disabled:opacity-30 disabled:cursor-not-allowed disabled:no-underline"
            >
              {isResetting ? "Resetting…" : "Reset device connections"}
            </button>
          </aside>

          {/* Center: chart */}
          <main className="flex-1 flex flex-col gap-4 p-4 overflow-hidden min-h-0">
            <div className="flex-1 min-h-0">
              <RealtimeChart samples={liveSamples} devices={devices} />
            </div>

            {/* Label panel */}
            <div className="shrink-0 glass-panel p-3">
              {labelError && <p className="text-xs text-red-400 mb-1">{labelError}</p>}
              <LabelingPanel
                activeLabel={activeLabel}
                onLabel={handleLabel}
                disabled={!isRecording}
              />
            </div>

            {/* Integrity report */}
            {integrityReport && (
              <div className="shrink-0 glass-panel p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs text-gray-400 font-bold uppercase tracking-wider">
                    Last Session Report
                  </span>
                  <button
                    onClick={() => setIntegrityReport(null)}
                    className="btn-glass text-xs text-gray-400 px-2 py-0.5"
                  >
                    ✕ Dismiss
                  </button>
                </div>
                <div className="max-h-44 overflow-y-auto">
                  <SafeBoundary what="integrity report">
                    <IntegrityReport report={integrityReport as unknown as Parameters<typeof IntegrityReport>[0]["report"]} />
                  </SafeBoundary>
                </div>
              </div>
            )}
          </main>

          {/* Right: cameras (1–5) */}
          <aside className="glass-rail w-72 shrink-0 border-l border-white/10 p-4 flex flex-col gap-3 overflow-y-auto">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider">Cameras</h3>
              <span className={`text-[11px] font-bold ${camStatus.ok ? "text-green-400" : "text-red-400"}`}>
                {camStatus.ready}/{camStatus.total}
              </span>
            </div>
            <MultiCameraRecorder
              ref={camRef}
              onStatusChange={setCamStatus}
              onRecordingError={message => setCameraStartError(`Camera recording could not start: ${message}`)}
              backendIp={backendIp}
              sessionId={isRecording ? sessionId : ""}
              disabled={isRecording}
            />
            {cameraStartError && <p className="text-xs text-red-400">{cameraStartError}</p>}
            {!camStatus.ok && (
              <p className="text-xs text-red-400">
                {camStatus.total === 0
                  ? "Select at least one camera — required for recording"
                  : `${camStatus.total - camStatus.ready} camera(s) not ready`}
              </p>
            )}
          </aside>
        </div>
      </div>

      {/* Reset connections — confirm modal, only reachable when not recording */}
      {showReset && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={() => setShowReset(false)}>
          <div
            className="max-w-sm w-full glass-panel p-5 space-y-4"
            onClick={e => e.stopPropagation()}
          >
            <h2 className="text-base font-bold text-red-400">Reset all connections?</h2>
            <p className="text-xs text-gray-300 leading-relaxed">
              This will forcibly close every connected device and clear all previous
              connectivity warnings (offline gaps and “no data” flags, plus the event log).
            </p>
            <p className="text-xs text-gray-500">
              Only affects live device connections — recovery/rescue files are not touched.
            </p>
            <p className="text-xs text-gray-500">
              Not available while a session is recording. Devices will need to reconnect
              to resume streaming.
            </p>
            {resetError && <p className="text-xs text-red-400">{resetError}</p>}
            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={() => setShowReset(false)}
                disabled={isResetting}
                className="btn-glass px-3 py-1.5 text-xs text-gray-300 disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                onClick={handleReset}
                disabled={isResetting}
                className="btn-danger px-3 py-1.5 text-xs font-bold disabled:opacity-50"
              >
                {isResetting ? "Resetting…" : "Reset now"}
              </button>
            </div>
          </div>
        </div>
      )}

      <VideoRecoveryModal
        open={showVideoRecovery}
        onClose={() => setShowVideoRecovery(false)}
      />

      <RecoveryModal
        backendIp={backendIp}
        open={showRecovery}
        onClose={() => setShowRecovery(false)}
      />

      {/* End-of-session export — non-dismissible until a .zip download has completed.
          The cleared video backup only happens AFTER a successful download so footage
          survives a failed/aborted download until the next session anyway (see
          video_backup.ts clearAllChunks). */}
      <SafeBoundary
        what="end-of-session export dialog"
        recoveryHref={endSession ? `http://${backendIp}:8000/export/${encodeURIComponent(endSession.sessionId)}/bundle/file` : undefined}
      >
        <EndSessionModal
          session={endSession}
          videoResults={endVideoResults}
          missed={endMissed}
          backendIp={backendIp}
          recheckTick={endRecheckTick}
          finalize={finalize}
          onClose={handleEndSessionClose}
          onDownloadComplete={(sid) => {
            localStorage.setItem(ACKED_END_KEY, sid);
            localStorage.removeItem(PENDING_END_KEY);
            void clearChunks(sid);
          }}
        />
      </SafeBoundary>
    </>
  );
}

async function probeBackend(ip: string): Promise<
  { ok: true; lanIp?: string; buildId?: string } | { ok: false; reason: string }
> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2000);
    const res = await fetch(`http://${ip}:8000/health`, { signal: ctrl.signal });
    clearTimeout(t);
    if (!res.ok) return { ok: false, reason: `Backend answered HTTP ${res.status}` };
    const j = await res.json();
    return { ok: true, lanIp: j.lan_ip, buildId: j.build_id };
  } catch {
    return { ok: false, reason: "No HTTP response (backend not started, wrong IP, or firewall/subnet)" };
  }
}
