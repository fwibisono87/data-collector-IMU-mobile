"use client";
import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";

import { wsClient, type SessionState, type DeviceInfo, type StateUpdate } from "@/lib/ws_client";
import { armAudio } from "@/lib/alert_sound";
import StatusBanner from "@/components/StatusBanner";
import SessionForm from "@/components/SessionForm";
import PreflightPanel from "@/components/PreflightPanel";
import LabelingPanel from "@/components/LabelingPanel";
import IntegrityReport from "@/components/IntegrityReport";
import DevicePanel from "@/components/DevicePanel";
import AlertCenter from "@/components/AlertCenter";
// Direct import — dynamic() breaks forwardRef so camRef.current would be null.
import MultiCameraRecorder, {
  type MultiCameraRecorderHandle,
  type CameraStatus,
} from "@/components/MultiCameraRecorder";
import AmbientBackdrop from "@/components/AmbientBackdrop";
import RecoveryModal from "@/components/RecoveryModal";
import { clearChunks } from "@/lib/video_backup";
import EndSessionModal, {
  type EndSessionInfo,
  type EndSessionVideoResult,
} from "@/components/EndSessionModal";

// ECharts uses browser APIs — dynamic import keeps SSR safe.
const RealtimeChart = dynamic(() => import("@/components/RealtimeChart"), { ssr: false });

// ── Types ─────────────────────────────────────────────────────────────────────
type AppView = "connect" | "dashboard";
type Sample = { acc: number[]; gyro: number[]; ts: number };

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
  const [camStatus, setCamStatus] = useState<CameraStatus>({ ready: 0, total: 0, ok: false });
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

  // End-of-session export modal — replaces the immediate per-camera downloads.
  const [endSession, setEndSession] = useState<EndSessionInfo | null>(null);
  const [endVideoResults, setEndVideoResults] = useState<EndSessionVideoResult[]>([]);
  const [endMissed, setEndMissed] = useState<string[]>([]);
  const [endRecheckTick, setEndRecheckTick] = useState(0);
  const endSessionOpenRef = useRef(false);
  useEffect(() => { endSessionOpenRef.current = endSession !== null; }, [endSession]);

  const isRecording = sessionState === "RECORDING";
  // Derive online count directly from devices — single source of truth.
  const onlineCount = devices.filter(d => d.is_online).length;
  const prefightAllPass =
    isWsConnected &&
    onlineCount > 0 &&
    subject.trim().length > 0 &&
    sessionTag.trim().length > 0 &&
    operator.trim().length > 0 &&
    camStatus.ok;

  // ── WS event subscriptions ─────────────────────────────────────────────────
  useEffect(() => {
    const unsub = wsClient.onMessage((msg) => {
      if (msg.type === "STATE_UPDATE") {
        const su = msg as StateUpdate & {
          quorum?: { connected: number; roles: string[] };
          scheduled_start_ms?: number;
        };
        setSessionState(su.state);
        setSessionId(su.session_id ?? "");
        // STATE_UPDATE always carries the authoritative, complete device list. Apply it
        // verbatim — including an empty list — so pruned/offline devices and a backend
        // restart clear stale cards instead of lingering until a manual reload.
        if (su.devices) setDevices(su.devices);
        if (su.quorum) setQuorum(su.quorum);
        if (su.integrity_report) setIntegrityReport(su.integrity_report);

        // Coordinated webcam start (CLAUDE.md §22.5)
        if (su.state === "RECORDING" && su.scheduled_start_ms) {
          const delay = su.scheduled_start_ms - Date.now();
          setTimeout(() => {
            camRef.current?.startRecording(su.session_id || String(Date.now()));
          }, Math.max(0, delay));
        }
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
  }, []);

  // ── Auto-reconnect on mount ────────────────────────────────────────────────
  useEffect(() => {
    const saved = localStorage.getItem("backendIp");
    if (!saved) return;

    setBackendIp(saved);
    wsClient.connect(saved);

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

  // ── Connect ────────────────────────────────────────────────────────────────
  const handleConnect = () => {
    armAudio();   // the click is the required user gesture before browser audio can play
    setConnectError("");
    wsClient.connect(backendIp);

    // Poll until WS opens (max 5s).
    let tries = 0;
    const poll = setInterval(() => {
      if (wsClient.isConnected) {
        clearInterval(poll);
        localStorage.setItem("backendIp", backendIp);
        setIsWsConnected(true);
        setView("dashboard");
        wsClient.getState();
      } else if (++tries > 25) {
        clearInterval(poll);
        probeBackend(backendIp).then(probe => {
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
    try {
      await wsClient.startSession(subject, sessionTag, operator);
      // Webcam start is triggered by STATE_UPDATE with scheduled_start_ms
      // for coordinated sync with mobile devices (CLAUDE.md §22.5)
    } catch (e) {
      alert(`Start failed: ${e}`);
    }
  };

  const handleStop = async () => {
    if (isStopping) return;          // guard double-click → no spurious "Not recording" alert
    setIsStopping(true);
    // Capture identity BEFORE the stop call — a late STATE_UPDATE broadcast after the ACK
    // could otherwise describe the session differently than the one we just ended.
    const ended = { sessionId, subject, sessionTag, operator };
    let results: EndSessionVideoResult[] = [];
    let missed: string[] = [];
    // Stop the cameras first, but never let a camera-finalisation throw strand the backend in
    // RECORDING. A throw here is caught and recorded so the session can still be stopped.
    try {
      const out = (await camRef.current?.stopRecording()) ?? { results: [], missed: [] };
      results = out.results;
      missed = out.missed;
    } catch (e) {
      console.error("camera finalisation failed (session stop will still run)", e);
    }
    // Always tell the backend the session ended, in its OWN try/catch. This must execute even
    // if the camera stop above threw — the previous code path aborted before stopSession and
    // left the backend recording forever despite the "cannot strand" comment. [Finding B]
    let stopError = "";
    try {
      await wsClient.stopSession("operator_stop");
    } catch (e) {
      stopError = String(e);
      console.error("session stop on backend failed", e);
    }
    // No immediate downloads — the end-of-session modal handles artifacts+video as one
    // zip, and cannot be dismissed until a download has completed.
    setEndSession(ended);
    setEndVideoResults(results);
    setEndMissed(missed);
    setIsStopping(false);
    if (stopError) alert(`Session stop reported a problem: ${stopError}`);
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
            <PreflightPanel
              isWsConnected={isWsConnected}
              devices={devices}
              subject={subject}
              sessionTag={sessionTag}
              operator={operator}
              camStatus={camStatus}
            />

            {/* Open the recover-from-devices modal */}
            <button
              onClick={() => setShowRecovery(true)}
              className="btn-glass w-full py-2 text-xs text-gray-300"
            >
              Download / merge phone rescue files
            </button>

            {/* Start / Stop button */}
            {!isRecording ? (
              <button
                onClick={handleStart}
                disabled={!prefightAllPass}
                className="btn-success w-full py-2 font-bold text-sm disabled:opacity-30 disabled:cursor-not-allowed"
              >
                ▶ START SESSION
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
                  <IntegrityReport report={integrityReport as unknown as Parameters<typeof IntegrityReport>[0]["report"]} />
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
            <MultiCameraRecorder ref={camRef} onStatusChange={setCamStatus} disabled={isRecording} />
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

      <RecoveryModal
        backendIp={backendIp}
        open={showRecovery}
        onClose={() => setShowRecovery(false)}
      />

      {/* End-of-session export — non-dismissible until a .zip download has completed.
          The cleared video backup only happens AFTER a successful download so footage
          survives a failed/aborted download until the next session anyway (see
          video_backup.ts clearAllChunks). */}
      <EndSessionModal
        session={endSession}
        videoResults={endVideoResults}
        missed={endMissed}
        backendIp={backendIp}
        recheckTick={endRecheckTick}
        onClose={() => setEndSession(null)}
        onDownloadComplete={(sid) => { void clearChunks(sid); }}
      />
    </>
  );
}

async function probeBackend(ip: string): Promise<
  { ok: true; lanIp?: string } | { ok: false; reason: string }
> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2000);
    const res = await fetch(`http://${ip}:8000/health`, { signal: ctrl.signal });
    clearTimeout(t);
    if (!res.ok) return { ok: false, reason: `Backend answered HTTP ${res.status}` };
    const j = await res.json();
    return { ok: true, lanIp: j.lan_ip };
  } catch {
    return { ok: false, reason: "No HTTP response (backend not started, wrong IP, or firewall/subnet)" };
  }
}
