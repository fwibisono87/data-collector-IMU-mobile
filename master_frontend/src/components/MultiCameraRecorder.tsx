"use client";
import { useCallback, useEffect, useRef, useState, useImperativeHandle, forwardRef } from "react";
import { saveChunk, loadChunks, clearAllChunks } from "@/lib/video_backup";
import { finalizeWebm } from "@/lib/webm_seekable";

// ── Public contract (consumed by page.tsx) ──────────────────────────────────
export interface CameraResult { camId: string; deviceId: string; label: string; blob: Blob; mime: string; }
export interface CameraStatus { ready: number; total: number; ok: boolean; }
export interface StopOutcome { results: CameraResult[]; missed: string[]; }
export interface MultiCameraRecorderHandle {
  startRecording: (sessionId: string) => Promise<void>;
  stopRecording: () => Promise<StopOutcome>;
}
interface Props {
  onStatusChange: (status: CameraStatus) => void;
  disabled: boolean; // true while RECORDING — lock camera selection
}

const MAX_CAMERAS = 5;
const TIMESLICE_MS = 1000;
// Prefer WebM (VP9→VP8). Chrome/Edge MediaRecorder emits *fragmented* MP4 that desktop
// players (Windows Media Player, QuickTime) can't open, even though it plays in-browser —
// so MP4 is deliberately the last resort, kept only so a WebM-less browser (Safari) still
// records rather than failing. WebM output gets its duration patched on stop (see stopFn),
// making it fully seekable in VLC / browsers / modern players. Video-only: no audio codec.
const CODEC_PRIORITY = [
  "video/webm;codecs=vp9",
  "video/webm;codecs=vp8",
  "video/webm",
  "video/mp4",
];

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
}

function CameraTile({ camId, deviceId, label, deviceEpoch, register, onStatus }: TileProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const mediaRef = useRef<MediaRecorder | null>(null);
  const sessionRef = useRef<string>("");
  const chunkIndexRef = useRef(0);
  const pendingSavesRef = useRef<Promise<void>[]>([]); // in-flight chunk writes to await on stop
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
  }, [deviceId, camId, onStatus]);

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
    if (!streamRef.current) return;
    sessionRef.current = sessionId;
    chunkIndexRef.current = 0;
    pendingSavesRef.current = [];
    const mime = CODEC_PRIORITY.find(m => MediaRecorder.isTypeSupported(m)) ?? "";
    const recorder = new MediaRecorder(streamRef.current, mime ? { mimeType: mime } : {});
    mediaRef.current = recorder;
    // Track each write's promise (don't await inside the handler): stop() flushes a final
    // chunk, and we must await that write before reading back — otherwise the last ~1s is lost.
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) {
        pendingSavesRef.current.push(saveChunk(sessionId, camId, chunkIndexRef.current++, e.data));
      }
    };
    recorder.start(TIMESLICE_MS);
    setIsRecording(true);
    setFlash(true);                          // operator-facing sync cue (parity with old)
    setTimeout(() => setFlash(false), 100);
  };
  const stopFn = async (): Promise<CameraResult | null> => {
    const recorder = mediaRef.current;
    if (!recorder || recorder.state === "inactive") return null;
    await new Promise<void>(resolve => { recorder.onstop = () => resolve(); recorder.stop(); });
    setIsRecording(false);
    // stop() fires a final dataavailable before onstop; wait for every chunk write to commit
    // so the reassembled blob includes the tail (fixes the dropped last second).
    await Promise.all(pendingSavesRef.current);
    const chunks = await loadChunks(sessionRef.current, camId);
    if (chunks.length === 0) return null;
    // MediaRecorder writes WebM as a live stream with no header duration/seek info → players
    // can't determine length and the tail is often misreported as broken. Rewrite the metadata
    // (SeekHead/Info/Cues) via the battle-tested ts-ebml finalizer so the file is fully playable
    // and seekable, preserving every cluster byte. MP4 fallback (Safari) is left untouched. The
    // finalizer always falls back to the raw concatenation rather than losing footage.
    const isWebm = (chunks[0].type || "video/webm").includes("webm");
    const blob = isWebm
      ? (await finalizeWebm(chunks)).blob
      : new Blob(chunks, { type: chunks[0].type || "video/webm" });
    // Do NOT clear here — chunks stay in IndexedDB so footage survives a blocked/aborted
    // download. They are GC'd at the start of the NEXT session (see startRecording). [Finding A]
    return { camId, deviceId, label, blob, mime: blob.type };
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
  ({ onStatusChange, disabled }, ref) => {
    const [cameras, setCameras] = useState<MediaDeviceInfo[]>([]);
    const [active, setActive] = useState<ActiveCam[]>([]);
    const [permError, setPermError] = useState("");
    const [deviceEpoch, setDeviceEpoch] = useState(0); // bumped on devicechange → tiles re-acquire
    const tilesRef = useRef<Map<string, TileHandle>>(new Map());
    const statusRef = useRef<Map<string, boolean>>(new Map());
    const activeRef = useRef<ActiveCam[]>([]);
    const disabledRef = useRef(disabled);
    disabledRef.current = disabled;             // RECORDING lock, readable in stable handlers
    const grantedRef = useRef(false);           // true once camera permission is granted

    // Compute aggregate readiness from refs (so the callbacks below stay stable).
    const emitStatus = useCallback(() => {
      const total = activeRef.current.length;
      let ready = 0;
      activeRef.current.forEach(c => { if (statusRef.current.get(c.camId)) ready++; });
      onStatusChange({ ready, total, ok: total >= 1 && ready === total });
    }, [onStatusChange]);

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
            onStatusChange({ ready: 0, total: 0, ok: false });
            return;
          }
          setPermError("Camera access blocked — enable it, then click Retry");
          onStatusChange({ ready: 0, total: 0, ok: false });
          console.error("camera permission probe failed", e);
          return;
        }
      }
      const devs = await refreshDevices();
      // Auto-select the first camera ONLY if nothing is selected yet (parity with old default,
      // and recovers a zero-camera startup once a camera appears).
      setActive(prev => {
        if (prev.length > 0 || devs.length === 0) return prev;
        return [{ camId: "cam1", deviceId: devs[0].deviceId, label: devs[0].label || "camera 1" }];
      });
    }, [refreshDevices, onStatusChange]);

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
          return prev.filter(a => a.deviceId !== dev.deviceId);
        }
        if (prev.length >= MAX_CAMERAS) return prev; // cap
        // assign the lowest free camId (cam1..cam5)
        let camId = `cam${MAX_CAMERAS}`;
        for (let i = 1; i <= MAX_CAMERAS; i++) {
          const c = `cam${i}`;
          if (!prev.some(a => a.camId === c)) { camId = c; break; }
        }
        return [...prev, { camId, deviceId: dev.deviceId, label: dev.label || camId }];
      });
    };

    useImperativeHandle(ref, () => ({
      // Fan out to all live tiles in the SAME callback → synchronized start.
      async startRecording(sessionId: string) {
        // Deferred-clear point: free the PREVIOUS session's backups now that a new session is
        // starting. Must finish before any tile starts writing chunks. [Finding A]
        await clearAllChunks();
        await Promise.all(Array.from(tilesRef.current.values()).map(t => t.start(sessionId)));
      },
      async stopRecording(): Promise<StopOutcome> {
        const entries = Array.from(tilesRef.current.entries());
        const raw = await Promise.all(entries.map(([, t]) => t.stop()));
        const results: CameraResult[] = [];
        const missed: string[] = [];
        raw.forEach((r, i) => { if (r !== null) results.push(r); else missed.push(entries[i][0]); });
        return { results, missed };
      },
    }), []);

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
