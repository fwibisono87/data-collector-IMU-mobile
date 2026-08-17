"use client";
import { useCallback, useEffect, useRef, useState, useImperativeHandle, forwardRef } from "react";
import {
  saveChunk,
  clearConfirmedChunks,
  nextChunkIndex,
  recordCameraEvent,
  listAllChunkGroups,
  listCameraEvents,
} from "@/lib/video_backup";

// ── Public contract (consumed by page.tsx) ──────────────────────────────────
export interface CameraResult {
  camId: string; deviceId: string; label: string; mime: string;
  sessionId: string; chunkCount: number;
  startedAtMs: number; flashAtMs: number; stoppedAtMs: number;
}
export interface CameraStatus { ready: number; total: number; ok: boolean; restoring: boolean; }
export interface StopOutcome { results: CameraResult[]; missed: string[]; }
export interface MultiCameraRecorderHandle {
  startRecording: (sessionId: string) => Promise<void>;
  stopRecording: () => Promise<StopOutcome>;
}
interface Props {
  onStatusChange: (status: CameraStatus) => void;
  onRecordingError?: (message: string) => void;
  backendIp: string;
  sessionId?: string;
  disabled: boolean; // true while RECORDING — lock camera selection
}

const MAX_CAMERAS = 5;
const TIMESLICE_MS = 1000;
const STOP_TIMEOUT_MS = 10_000;
// Prefer WebM (VP9→VP8). Chrome/Edge MediaRecorder emits *fragmented* MP4 that desktop
// players (Windows Media Player, QuickTime) can't open, even though it plays in-browser —
// so MP4 is deliberately the last resort, kept only so a WebM-less browser (Safari) still
// records rather than failing. The raw chunks are kept in IndexedDB and streamed straight to
// disk by the export modal (see video_backup.streamChunks) — no in-memory reassembly. The
// recorder simply reports the codec it used (mimeType) so the export can name the file.
// Video-only: no audio codec.
const CODEC_PRIORITY = [
  "video/webm;codecs=vp9",
  "video/webm;codecs=vp8",
  "video/webm",
  "video/mp4",
];
const CAMERA_SELECTION_KEY = "imu.camera-selection.v1";

interface ActiveCam { camId: string; deviceId: string; label: string; }
interface TileHandle {
  start: (sessionId: string) => Promise<void>;
  stop: () => Promise<CameraResult | null>;
}

// ── One physical camera: its own stream, preview, recorder, chunk index ──────
interface TileProps {
  camId: string;
  deviceId: string;
  label: string;
  deviceEpoch: number; // bumped by manager on devicechange → lets a dead tile re-acquire
  register: (camId: string, handle: TileHandle | null) => void;
  onStatus: (camId: string, live: boolean) => void;
  backendIp: string;
}

function postCameraMark(
  backendIp: string,
  sessionId: string,
  body: Record<string, unknown>,
): void {
  if (!backendIp) return;
  void fetch(`http://${backendIp}:8000/cameras/${encodeURIComponent(sessionId)}/mark`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).catch(error => console.warn("camera anchor not persisted", error));
}

function CameraTile({ camId, deviceId, label, deviceEpoch, register, onStatus, backendIp }: TileProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const mediaRef = useRef<MediaRecorder | null>(null);
  const sessionRef = useRef<string>("");
  const chunkIndexRef = useRef(0);
  const pendingSavesRef = useRef(new Set<Promise<void>>()); // unresolved writes only
  const writeErrorRef = useRef<unknown>(null);
  const startedAtRef = useRef(0);
  const flashAtRef = useRef(0);
  const liveRef = useRef(false); // true while this slot has a working stream (gates re-acquire)
  const [isRecording, setIsRecording] = useState(false);
  const [flash, setFlash] = useState(false);
  const [detail, setDetail] = useState("opening…");

  // Acquire (or re-acquire) this slot's dedicated stream. Callers only invoke it when the
  // slot is NOT already live, so a healthy camera is never torn down → no reopen churn.
  const acquire = useCallback(async (shouldAbort: () => boolean): Promise<void> => {
    try {
      setDetail("opening…");
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { deviceId: { exact: deviceId }, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      if (shouldAbort()) { stream.getTracks().forEach(t => t.stop()); return; }
      streamRef.current = stream;
      liveRef.current = true;
      const track = stream.getVideoTracks()[0];
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        videoRef.current.muted = true;
        await videoRef.current.play().catch(() => {});
      }
      // A physical unplug ends the track. Mark the slot dead (fail loud) so the next
      // devicechange (reconnect) re-acquires it instead of leaving a black preview.
      track?.addEventListener("ended", () => {
        liveRef.current = false;
        streamRef.current = null;
        setDetail("disconnected — retrying on reconnect");
        onStatus(camId, false);
        if (sessionRef.current) {
          void recordCameraEvent(sessionRef.current, camId, "track_ended", "MediaStreamTrack ended");
          postCameraMark(backendIp, sessionRef.current, {
            cam_id: camId, event: "track_ended", ts_ms: Date.now(), device_id: deviceId, label,
          });
        }
      });
      const s = track?.getSettings();
      setDetail(s?.width ? `${s.width}×${s.height}` : "live");
      onStatus(camId, true);
    } catch (e) {
      // Fail loud: surface the failed camera so preflight blocks (CLAUDE.md "Fail Loud").
      if (!shouldAbort()) {
        liveRef.current = false;
        streamRef.current = null;
        setDetail("open failed");
        onStatus(camId, false);
        console.error(`cam ${camId} open failed`, e);
      }
    }
  }, [deviceId, camId, label, onStatus, backendIp]);

  // Initial open (deviceId/camId/acquire are all stable, so this runs once per slot) +
  // teardown on unmount.
  useEffect(() => {
    let cancelled = false;
    acquire(() => cancelled);
    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach(t => t.stop());
      streamRef.current = null;
      liveRef.current = false;
      onStatus(camId, false);
    };
  }, [acquire, camId, onStatus]);

  // Reconnect recovery: when the device set changes (hot-plug), re-acquire ONLY if this
  // slot's stream has died. A live tile returns immediately → no churn on healthy cameras.
  useEffect(() => {
    if (deviceEpoch === 0 || liveRef.current) return;
    let cancelled = false;
    acquire(() => cancelled);
    return () => { cancelled = true; };
  }, [deviceEpoch, acquire]);

  // start/stop read the latest stream/recorder via refs; the registered handle is stable.
  const startFn = async (sessionId: string) => {
    if (!streamRef.current) throw new Error(`${camId} camera stream is unavailable`);
    if (mediaRef.current && mediaRef.current.state !== "inactive") return;
    sessionRef.current = sessionId;
    chunkIndexRef.current = await nextChunkIndex(sessionId, camId);
    pendingSavesRef.current.clear();
    writeErrorRef.current = null;
    const mime = CODEC_PRIORITY.find(m => MediaRecorder.isTypeSupported(m)) ?? "";
    const recorder = new MediaRecorder(streamRef.current, mime ? { mimeType: mime } : {});
    mediaRef.current = recorder;
    // Track each write's promise (don't await inside the handler): stop() flushes a final
    // chunk, and we must await that write before reading back — otherwise the last ~1s is lost.
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) {
        const write = saveChunk(sessionId, camId, chunkIndexRef.current++, e.data)
          .catch((error) => {
            writeErrorRef.current ??= error;
            void recordCameraEvent(sessionId, camId, "write_error", String(error));
          })
          .finally(() => pendingSavesRef.current.delete(write));
        pendingSavesRef.current.add(write);
      }
    };
    recorder.onerror = (event) => {
      const detail = String((event as ErrorEvent).error ?? "MediaRecorder error");
      writeErrorRef.current ??= new Error(detail);
      void recordCameraEvent(sessionId, camId, "write_error", detail);
      postCameraMark(backendIp, sessionId, {
        cam_id: camId, event: "write_error", ts_ms: Date.now(), device_id: deviceId, label,
      });
    };
    recorder.start(TIMESLICE_MS);
    startedAtRef.current = Date.now();
    setIsRecording(true);
    flashAtRef.current = Date.now();
    void recordCameraEvent(sessionId, camId, "started", `mime=${mime};chunk_index=${chunkIndexRef.current}`);
    postCameraMark(backendIp, sessionId, {
      cam_id: camId, event: "started", ts_ms: startedAtRef.current,
      device_id: deviceId, label, mime,
    });
    postCameraMark(backendIp, sessionId, {
      cam_id: camId, event: "flash", ts_ms: flashAtRef.current,
      device_id: deviceId, label, mime,
    });
    setFlash(true);                          // operator-facing sync cue (parity with old)
    setTimeout(() => setFlash(false), 100);
  };
  const recoverStoppedRecording = async (recorder: MediaRecorder): Promise<CameraResult | null> => {
    const sessionId = sessionRef.current;
    if (!sessionId) return null;
    if (writeErrorRef.current) throw new Error(`${camId} video recorder failed: ${String(writeErrorRef.current)}`);

    // MediaRecorder can become inactive without our stop() callback winning the race
    // (track/device teardown, browser lifecycle, or a renderer interruption). The chunks
    // are still the authoritative footage. Recover a durable stop marker from that ledger
    // instead of reporting a missing camera and exporting stopped_at_ms=0.
    const group = (await listAllChunkGroups()).find(
      g => g.sessionId === sessionId && g.camId === camId && g.chunks > 0,
    );
    if (!group) return null;
    const events = await listCameraEvents(sessionId);
    const existingStop = events
      .filter(e => e.camId === camId && e.type === "stopped")
      .slice(-1)[0];
    const stoppedAtMs = existingStop?.atMs ?? Date.now();
    if (!existingStop) {
      await recordCameraEvent(sessionId, camId, "stopped", `chunks=${group.chunks};recovered_inactive_recorder`);
      postCameraMark(backendIp, sessionId, {
        cam_id: camId, event: "stopped", ts_ms: stoppedAtMs, device_id: deviceId, label,
      });
    }
    setIsRecording(false);
    return {
      camId, deviceId, label, mime: recorder.mimeType || "video/webm", sessionId,
      chunkCount: group.chunks, startedAtMs: startedAtRef.current,
      flashAtMs: flashAtRef.current, stoppedAtMs,
    };
  };

  const stopFn = async (): Promise<CameraResult | null> => {
    const recorder = mediaRef.current;
    if (!recorder) return null;
    if (recorder.state === "inactive") {
      return recoverStoppedRecording(recorder);
    }
    const mime = recorder.mimeType || "";
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error(`${camId} did not stop within ${STOP_TIMEOUT_MS / 1000}s`)), STOP_TIMEOUT_MS);
      recorder.onstop = () => { window.clearTimeout(timeout); resolve(); };
      try { recorder.stop(); } catch (error) { window.clearTimeout(timeout); reject(error); }
    });
    const stoppedAtMs = Date.now();
    setIsRecording(false);
    // stop() fires a final dataavailable before onstop. Await every chunk write to commit
    // before reporting the count, so the tail is flushed to IndexedDB (fixes the dropped
    // last second). We no longer reassemble the footage here — the raw chunks stay in
    // IndexedDB and are streamed straight to disk by the export modal (streamChunks),
    // avoiding the two full-heap copies that exhausted the renderer.
    await Promise.all(Array.from(pendingSavesRef.current));
    if (writeErrorRef.current) throw new Error(`${camId} video backup failed: ${String(writeErrorRef.current)}`);
    const chunkCount = chunkIndexRef.current;
    if (chunkCount === 0) return null;
    // Do NOT clear here — chunks stay in IndexedDB so footage survives a blocked/aborted
    // download. They are GC'd at the start of the NEXT session (see startRecording). [Finding A]
    // The end-session modal reads lifecycle events immediately after every camera stop
    // resolves. Await the IndexedDB transaction so a successful stop always has a durable
    // marker before the modal evaluates camera integrity.
    await recordCameraEvent(sessionRef.current, camId, "stopped", `chunks=${chunkCount}`);
    postCameraMark(backendIp, sessionRef.current, {
      cam_id: camId, event: "stopped", ts_ms: stoppedAtMs,
      device_id: deviceId, label, mime,
    });
    return { camId, deviceId, label, mime, sessionId: sessionRef.current, chunkCount,
             startedAtMs: startedAtRef.current, flashAtMs: flashAtRef.current, stoppedAtMs };
  };
  const startRef = useRef(startFn); startRef.current = startFn;
  const stopRef = useRef(stopFn); stopRef.current = stopFn;

  // Register once per tile; methods always call the freshest closure via refs.
  useEffect(() => {
    register(camId, { start: (s) => startRef.current(s), stop: () => stopRef.current() });
    return () => register(camId, null);
  }, [camId, register]);

  return (
    <div className="relative rounded-lg overflow-hidden bg-black border border-white/10">
      {flash && <div className="absolute inset-0 bg-white z-10 pointer-events-none" />}
      <video ref={videoRef} className="w-full aspect-video object-cover" playsInline muted />
      <div className="absolute top-1 left-1 flex items-center gap-1 bg-black/40 backdrop-blur-md border border-white/10 rounded-md px-1.5 py-0.5 text-[10px] text-gray-200">
        {isRecording && <span className="animate-pulse text-red-400">●</span>}
        <span className="font-bold">{camId}</span>
        <span className="opacity-60 max-w-[90px] truncate">{label}</span>
      </div>
      <div className="absolute bottom-1 right-1 bg-black/40 backdrop-blur-md rounded-md px-1 text-[10px] tabular-nums text-gray-400">
        {detail}
      </div>
    </div>
  );
}

// ── Manager: enumerate, select, aggregate readiness, fan out start/stop ──────
const MultiCameraRecorder = forwardRef<MultiCameraRecorderHandle, Props>(
  ({ onStatusChange, onRecordingError, backendIp, sessionId = "", disabled }, ref) => {
    const [cameras, setCameras] = useState<MediaDeviceInfo[]>([]);
    const [active, setActive] = useState<ActiveCam[]>([]);
    const [permError, setPermError] = useState("");
    const [deviceEpoch, setDeviceEpoch] = useState(0); // bumped on devicechange → tiles re-acquire
    const [restoringSession, setRestoringSession] = useState("");
    const tilesRef = useRef<Map<string, TileHandle>>(new Map());
    const statusRef = useRef<Map<string, boolean>>(new Map());
    const activeRef = useRef<ActiveCam[]>([]);
    const disabledRef = useRef(disabled);
    disabledRef.current = disabled;             // RECORDING lock, readable in stable handlers
    const grantedRef = useRef(false);           // true once camera permission is granted

    const readSavedSelection = useCallback((devs: MediaDeviceInfo[]): ActiveCam[] => {
      try {
        const saved = JSON.parse(localStorage.getItem(CAMERA_SELECTION_KEY) ?? "null");
        if (!Array.isArray(saved)) return [];
        return saved
          .map((v: unknown) => v as Partial<ActiveCam>)
          .filter(v => typeof v.deviceId === "string" && devs.some(d => d.deviceId === v.deviceId))
          .slice(0, MAX_CAMERAS)
          .map((v, i) => ({
            camId: typeof v.camId === "string" ? v.camId : `cam${i + 1}`,
            deviceId: v.deviceId!,
            label: v.label || devs.find(d => d.deviceId === v.deviceId)?.label || `camera ${i + 1}`,
          }));
      } catch {
        return [];
      }
    }, []);

    const persistSelection = useCallback((selection: ActiveCam[]) => {
      try { localStorage.setItem(CAMERA_SELECTION_KEY, JSON.stringify(selection)); } catch { /* best effort */ }
    }, []);

    // Compute aggregate readiness from refs (so the callbacks below stay stable).
    const emitStatus = useCallback(() => {
      const total = activeRef.current.length;
      let ready = 0;
      activeRef.current.forEach(c => { if (statusRef.current.get(c.camId)) ready++; });
      onStatusChange({
        ready,
        total,
        ok: total >= 1 && ready === total,
        restoring: restoringSession !== "",
      });
    }, [onStatusChange, restoringSession]);

    const handleTileStatus = useCallback((camId: string, live: boolean) => {
      statusRef.current.set(camId, live);
      emitStatus();
    }, [emitStatus]);

    const registerTile = useCallback((camId: string, h: TileHandle | null) => {
      if (h) tilesRef.current.set(camId, h); else tilesRef.current.delete(camId);
    }, []);

    // Mirror active → ref and re-emit status whenever the selection changes.
    useEffect(() => { activeRef.current = active; emitStatus(); }, [active, emitStatus]);

    // Re-enumerate the available video inputs. Labels are populated only after a prior
    // permission grant (the mount-time probe below), so this is safe to call repeatedly.
    const refreshDevices = useCallback(async (): Promise<MediaDeviceInfo[]> => {
      const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput");
      setCameras(devs);
      return devs;
    }, []);

    // Probe for permission (which unlocks device labels), then enumerate. Safe to call
    // repeatedly: once granted, getUserMedia resolves instantly with no prompt. Distinguishes
    // "no camera attached yet" (NOT an error — recovered on the next devicechange) from a real
    // denial, so starting the app with zero cameras and plugging one in later works WITHOUT a
    // restart/reload.
    const ensureReady = useCallback(async () => {
      if (!grantedRef.current) {
        try {
          const probe = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
          probe.getTracks().forEach(t => t.stop());   // release before opening exact devices
          grantedRef.current = true;
          setPermError("");
        } catch (e) {
          const name = (e as DOMException)?.name;
          if (name === "NotFoundError" || name === "DevicesNotFoundError" || name === "OverconstrainedError") {
            // No camera present yet — not a permission problem. devicechange will retry.
            setPermError("");
            await refreshDevices();
            onStatusChange({ ready: 0, total: 0, ok: false, restoring: false });
            return;
          }
          setPermError("Camera access blocked — enable it, then click Retry");
          onStatusChange({ ready: 0, total: 0, ok: false, restoring: false });
          console.error("camera permission probe failed", e);
          return;
        }
      }
      const devs = await refreshDevices();
      // Auto-select the first camera ONLY if nothing is selected yet (parity with old default,
      // and recovers a zero-camera startup once a camera appears).
      setActive(prev => {
        if (prev.length > 0 || devs.length === 0) return prev;
        const saved = readSavedSelection(devs);
        if (saved.length > 0) return saved;
        return [{ camId: "cam1", deviceId: devs[0].deviceId, label: devs[0].label || "camera 1" }];
      });
    }, [refreshDevices, onStatusChange, readSavedSelection]);

    // Mount: attempt to become ready once.
    useEffect(() => { ensureReady(); }, [ensureReady]);

    // Hot-plug: the browser fires `devicechange` when a camera is attached/removed. Re-enumerate
    // so newly connected webcams appear in the selector (fixes "won't auto-detect" and "can't
    // see a 2nd external cam"), and bump an epoch so a tile whose stream died re-acquires when
    // its device returns (fixes "reconnect → black"). The selector stays locked while RECORDING.
    useEffect(() => {
      const onDeviceChange = async () => {
        try {
          if (!grantedRef.current) {
            // Permission/labels still missing — a newly attached camera may now allow a
            // successful probe. Recovers a zero-camera or previously-denied startup.
            await ensureReady();
          } else {
            const devs = await refreshDevices();
            if (!disabledRef.current) {
              // NOT recording: drop any selected camera that physically went away so its tile
              // disappears ("disconnected → gone"). While RECORDING we keep the locked slot
              // (it shows "disconnected" and re-acquires on replug via deviceEpoch), so a
              // dropped angle is still reported on stop.
              const present = new Set(devs.map(d => d.deviceId));
              setActive(prev => {
                const kept = prev.filter(a => present.has(a.deviceId));
                if (kept.length === prev.length) return prev;
                prev.forEach(a => {
                  if (!present.has(a.deviceId)) {
                    statusRef.current.delete(a.camId);
                    tilesRef.current.delete(a.camId);
                  }
                });
                return kept;
              });
            }
          }
        } catch (e) {
          console.error("device re-enumeration failed", e);
        }
        setDeviceEpoch(n => n + 1);   // let live tiles whose stream died re-acquire on replug
      };
      navigator.mediaDevices.addEventListener("devicechange", onDeviceChange);
      return () => navigator.mediaDevices.removeEventListener("devicechange", onDeviceChange);
    }, [refreshDevices, ensureReady]);

    const toggleCamera = (dev: MediaDeviceInfo) => {
      if (disabled) return; // cannot change selection during RECORDING
      setActive(prev => {
        const existing = prev.find(a => a.deviceId === dev.deviceId);
        if (existing) {
          statusRef.current.delete(existing.camId);
          tilesRef.current.delete(existing.camId);
          const next = prev.filter(a => a.deviceId !== dev.deviceId);
          persistSelection(next);
          return next;
        }
        if (prev.length >= MAX_CAMERAS) return prev; // cap
        // assign the lowest free camId (cam1..cam5)
        let camId = `cam${MAX_CAMERAS}`;
        for (let i = 1; i <= MAX_CAMERAS; i++) {
          const c = `cam${i}`;
          if (!prev.some(a => a.camId === c)) { camId = c; break; }
        }
        const next = [...prev, { camId, deviceId: dev.deviceId, label: dev.label || camId }];
        persistSelection(next);
        return next;
      });
    };

    // Reconstruct the selected camera slots after a dashboard reload during RECORDING.
    // The browser-local selection is the fast path; the server anchors are the fallback
    // when the reload happened in a fresh browser profile/origin. Never replace a larger
    // current selection with a partial anchor snapshot that is still arriving.
    const restoredSessionRef = useRef("");
    useEffect(() => {
      if (!disabled || !sessionId || cameras.length === 0) {
        setRestoringSession("");
        return;
      }
      if (restoredSessionRef.current === sessionId) {
        setRestoringSession("");
        return;
      }
      setRestoringSession(sessionId);
      let cancelled = false;
      fetch(`http://${backendIp}:8000/cameras/${encodeURIComponent(sessionId)}`)
        .then(r => r.ok ? r.json() as Promise<{ cameras?: Array<Record<string, unknown>> }> : null)
        .then(data => {
          if (cancelled) return;
          const available = new Map(cameras.map(d => [d.deviceId, d]));
          const anchored = (data?.cameras ?? [])
            .map((c, i) => {
              const deviceId = String(c.device_id ?? "");
              const device = available.get(deviceId);
              if (!deviceId || !device) return null;
              return {
                camId: String(c.cam_id ?? `cam${i + 1}`),
                deviceId,
                label: String(c.browser_label ?? device.label ?? `camera ${i + 1}`),
              } satisfies ActiveCam;
            })
            .filter((c): c is ActiveCam => c !== null)
            .slice(0, MAX_CAMERAS);
          if (anchored.length > activeRef.current.length) {
            persistSelection(anchored);
            setActive(anchored);
          }
          restoredSessionRef.current = sessionId;
        })
        .catch(() => { /* local selection remains the recovery path */ })
        .finally(() => {
          if (!cancelled) setRestoringSession("");
        });
      return () => { cancelled = true; };
    }, [backendIp, cameras, disabled, persistSelection, sessionId]);

    useImperativeHandle(ref, () => ({
      // Fan out to all live tiles in the SAME callback → synchronized start.
      async startRecording(sessionId: string) {
        // Deferred-clear point: free the PREVIOUS session's backups now that a new session is
        // starting. Must finish before any tile starts writing chunks. [Finding A]
        //
        // Only sessions whose footage was CONFIRMED written to disk are dropped. Unsaved
        // footage is retained and surfaced by the recovery screen — wiping it here is what
        // would have made the 2026-08-07 crash unrecoverable.
        try {
          const { kept } = await clearConfirmedChunks();
          if (kept.length > 0) {
            console.warn(`video_backup: retained unsaved footage for session(s) ${kept.join(", ")}`);
          }
          const tiles = Array.from(tilesRef.current.values());
          if (tiles.length === 0) throw new Error("no camera recorder tiles are ready");
          await Promise.all(tiles.map(t => t.start(sessionId)));
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          onRecordingError?.(message);
          throw error;
        }
      },
      async stopRecording(): Promise<StopOutcome> {
        const entries = Array.from(tilesRef.current.entries());
        const raw = await Promise.all(entries.map(([, t]) => t.stop()));
        const results: CameraResult[] = [];
        const missed: string[] = [];
        raw.forEach((r, i) => { if (r !== null) results.push(r); else missed.push(entries[i][0]); });
        return { results, missed };
      },
    }), [onRecordingError]);

    return (
      <div className="flex flex-col gap-2">
        {/* Selector */}
        <div className="text-[11px]">
          {permError
            ? (
              <span className="text-red-400">
                {permError}{" "}
                <button onClick={() => ensureReady()} className="underline text-cyan-400 hover:text-cyan-300">
                  Retry
                </button>
              </span>
            )
            : <span className="text-gray-400">{active.length}/{MAX_CAMERAS} selected · {cameras.length} detected</span>}
        </div>
        <div className="space-y-1">
          {cameras.map((dev, i) => {
            const sel = active.find(a => a.deviceId === dev.deviceId);
            const capped = !sel && active.length >= MAX_CAMERAS;
            const lock = disabled || capped;
            return (
              <label
                key={dev.deviceId || i}
                className={`flex items-center gap-2 text-[11px] px-2 py-1 rounded border
                  ${sel ? "border-accent/60 bg-accent/10 text-white" : "border-white/10 text-gray-400"}
                  ${lock ? "opacity-40 cursor-not-allowed" : "cursor-pointer hover:border-accent/50"}`}
              >
                <input
                  type="checkbox"
                  className="accent-cyan-400"
                  checked={!!sel}
                  disabled={lock}
                  onChange={() => toggleCamera(dev)}
                />
                <span className="w-9 tabular-nums">{sel ? sel.camId : "—"}</span>
                <span className="flex-1 truncate">{dev.label || `Camera ${i + 1}`}</span>
              </label>
            );
          })}
          {cameras.length === 0 && !permError && (
            <p className="text-[11px] text-gray-600 italic">No cameras detected</p>
          )}
        </div>

        {/* Live previews */}
        <div className="space-y-2">
          {active.map(c => (
            <CameraTile
              key={c.camId}
              camId={c.camId}
              deviceId={c.deviceId}
              label={c.label}
              deviceEpoch={deviceEpoch}
              register={registerTile}
              onStatus={handleTileStatus}
              backendIp={backendIp}
            />
          ))}
          {active.length === 0 && !permError && (
            <p className="text-[11px] text-gray-600 italic">Select at least one camera</p>
          )}
        </div>
      </div>
    );
  },
);
MultiCameraRecorder.displayName = "MultiCameraRecorder";
export default MultiCameraRecorder;
