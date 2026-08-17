"use client";
import { useCallback, useEffect, useState } from "react";
import {
  fetchManifest,
  streamExportFile,
  postConsolidate,
  postBundle,
  dataBundleUrl,
  streamRecoveryFile,
  isDataKind,
  type ExportManifest,
} from "@/lib/export_client";
import { streamZipToDisk, canStreamSave, type StreamZipEntry } from "@/lib/zip_stream";
import {
  cameraByteSegments,
  streamCameraByteRange,
  markSessionSaved,
  isSessionSaved,
  invalidateSessionSaved,
  listAllChunkGroups,
  listCameraEvents,
  type ChunkGroup,
  type CameraEvent,
} from "@/lib/video_backup";
import { finalizeWebmStream, type FinalizeResult } from "@/lib/webm_seekable";

/** What one written video file turned out to be, recorded for the bundle and the UI. */
interface VideoFinalizeReport extends FinalizeResult {
  path: string;
  camId: string;
}

// ── Public contract ───────────────────────────────────────────────────────

export interface EndSessionInfo {
  sessionId: string;
  subject: string;
  sessionTag: string;
  operator: string;
}

export interface EndSessionVideoResult {
  camId: string;
  deviceId: string;
  label: string;
  mime: string;
  sessionId: string;
  chunkCount: number;
  startedAtMs: number;
  flashAtMs: number;
  stoppedAtMs: number;
}

interface Props {
  session: EndSessionInfo | null;   // null → hidden
  videoResults: EndSessionVideoResult[];
  missed: string[];
  backendIp: string;
  recheckTick: number;              // bump externally (e.g. LATE_DELIVERY) to re-fetch
  onClose: () => void;              // only reachable after a successful download
  onDownloadComplete: (sessionId: string) => void;
}

// Wait this long for phones to finish uploading rescue CSVs before consolidating.
const CONSOLIDATE_WAIT_MS = 60_000;
const POLL_MS = 3000;

async function waitFor<T>(
  operation: () => Promise<T>,
  deadline: number,
  onRetry?: (secondsLeft: number) => void,
): Promise<T> {
  let lastError: unknown;
  while (true) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw lastError;
      onRetry?.(Math.max(0, Math.ceil(remaining / 1000)));
      await new Promise(resolve => setTimeout(resolve, Math.min(POLL_MS, remaining)));
    }
  }
}

function _ext(r: EndSessionVideoResult): string {
  return r.mime.includes("mp4") ? "mp4" : "webm";
}

const STATUS_STYLE: Record<string, string> = {
  PASS:    "text-green-400 bg-green-500/10 border-green-500/30",
  PARTIAL: "text-yellow-400 bg-yellow-500/10 border-yellow-500/30",
  FAIL:    "text-red-400 bg-red-500/10 border-red-500/30",
  UNKNOWN: "text-gray-400 bg-white/5 border-white/10",
  NONE:    "text-gray-400 bg-white/5 border-white/10",
};

export default function EndSessionModal({
  session,
  videoResults,
  missed,
  backendIp,
  recheckTick,
  onClose,
  onDownloadComplete,
}: Props) {
  const [manifest, setManifest] = useState<ExportManifest | null>(null);
  const [dataError, setDataError] = useState("");
  const [phase, setPhase] = useState<"idle" | "waiting" | "consolidating">("idle");
  const [progressText, setProgressText] = useState("");
  const [consolidateResult, setConsolidateResult] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const [downloaded, setDownloaded] = useState(false);
  const [bundlePhase, setBundlePhase] = useState<"idle" | "running">("idle");
  const [bundleResult, setBundleResult] = useState("");
  const [cameraGroups, setCameraGroups] = useState<ChunkGroup[]>([]);
  const [cameraEvents, setCameraEvents] = useState<CameraEvent[]>([]);
  const sessionKey = session?.sessionId ?? "";

  // After a browser reload there is no in-memory CameraResult, but IndexedDB still has
  // the session's chunks. Reconstruct those cameras so the recovered footage is included
  // in the final export instead of being shown only in the separate recovery dialog.
  const eventsByCamera = new Map<string, CameraEvent[]>();
  for (const event of cameraEvents) {
    const list = eventsByCamera.get(event.camId) ?? [];
    list.push(event);
    eventsByCamera.set(event.camId, list);
  }
  const eventFor = (camId: string, type: CameraEvent["type"]): CameraEvent | undefined =>
    (eventsByCamera.get(camId) ?? []).filter(e => e.type === type).slice(-1)[0];

  const effectiveVideoResults: EndSessionVideoResult[] = [
    ...videoResults,
    ...cameraGroups
      .filter(g => !videoResults.some(v => v.camId === g.camId))
      .map(g => {
        const started = eventFor(g.camId, "started");
        const stopped = eventFor(g.camId, "stopped");
        const mime = started?.detail?.match(/(?:^|;)mime=([^;]+)/)?.[1] ?? "video/webm";
        return {
          camId: g.camId,
          deviceId: "unknown",
          label: g.camId,
          mime,
          sessionId: g.sessionId,
          chunkCount: g.chunks,
          startedAtMs: started?.atMs ?? 0,
          flashAtMs: started?.atMs ?? 0,
          stoppedAtMs: stopped?.atMs ?? 0,
        };
      }),
  ];
  const cameraProblems = [
    ...missed,
    ...cameraGroups.filter(g => g.hasHole).map(g => `${g.camId}: missing video chunks`),
    ...cameraEvents
      .filter(e => e.type === "track_ended" || e.type === "write_error")
      .map(e => `${e.camId}: ${e.type.replace("_", " ")}`),
    ...cameraGroups
      // The stop result is returned only after the IndexedDB stopped event has been
      // committed, but the modal can render before its first evidence read observes that
      // event. Prefer that in-memory proof while the short refresh below catches up.
      .filter(g => {
        const result = effectiveVideoResults.find(v => v.camId === g.camId);
        const hasStoppedResult = Boolean(result?.stoppedAtMs);
        return eventFor(g.camId, "started") && !eventFor(g.camId, "stopped") && !hasStoppedResult;
      })
      .map(g => `${g.camId}: no durable stop marker`),
  ];

  const handleServerBundle = useCallback(async () => {
    if (!session) return;
    setBundlePhase("running");
    setBundleResult("");
    try {
      const r = await waitFor(
        () => postBundle(backendIp, session.sessionId),
        Date.now() + CONSOLIDATE_WAIT_MS,
      );
      const mb = (r.size / 1048576).toFixed(1);
      setBundleResult(
        `✓ Saved on backend: ${r.path} (${mb} MB, ${r.entries.length} files). ` +
        `Video is not included — retrieve it from “Recover buffered video”.`,
      );
    } catch (e) {
      setBundleResult(`✕ Backend could not write the bundle: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBundlePhase("idle");
    }
  }, [session, backendIp]);

  const handleDataBundleDownload = useCallback(async () => {
    if (!session || bundlePhase === "running") return;
    setBundlePhase("running");
    setBundleResult("");
    try {
      // Build on the backend first. Unlike the browser ZIP, this has no dependency on a
      // PASS verdict or a manifest that happens to be available at this instant.
      await waitFor(
        () => postBundle(backendIp, session.sessionId),
        Date.now() + CONSOLIDATE_WAIT_MS,
      );
      const a = document.createElement("a");
      a.href = dataBundleUrl(backendIp, session.sessionId);
      a.download = `${session.sessionId}_bundle.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setBundleResult("✓ Data bundle download started. It contains every CSV/artifact available on the backend, including incomplete-session data.");
    } catch (e) {
      setBundleResult(`✕ Could not build/download data bundle: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBundlePhase("idle");
    }
  }, [session, backendIp, bundlePhase]);

  const loadManifest = useCallback(async () => {
    if (!session) return;
    setDataError("");
    setConsolidateResult("");
    setPhase("waiting");
    setProgressText("Waiting for backend finalization and phone rescue uploads…");
    try {
      const deadline = Date.now() + CONSOLIDATE_WAIT_MS;
      const m = await waitFor(
        () => fetchManifest(backendIp, session.sessionId),
        deadline,
        seconds => setProgressText(`Waiting for backend artifacts… ${seconds}s left`),
      );
      setManifest(m);
    } catch (e) {
      setManifest(null);
      setDataError(
        `Backend finalization is still unavailable (${e instanceof Error ? e.message : String(e)}). ` +
        "The recording may still be intact; use Re-check in a moment before treating it as missing.",
      );
    } finally {
      setPhase("idle");
      setProgressText("");
    }
  }, [session, backendIp]);

  const refreshCameraEvidence = useCallback(async () => {
    if (!session) return;
    const [groups, events] = await Promise.all([
      listAllChunkGroups(),
      listCameraEvents(session.sessionId),
    ]);
    setCameraGroups(groups.filter(g => g.sessionId === session.sessionId));
    setCameraEvents(events);
  }, [session]);

  // (Re)load whenever opened or externally poked (late delivery arrived). Stop markers
  // are written asynchronously to the browser ledger, so take a few bounded follow-up
  // snapshots; this removes a false "no durable stop marker" warning without keeping the
  // modal in a polling loop forever.
  useEffect(() => {
    if (!session) return;
    loadManifest();
    void refreshCameraEvidence().catch(() => {
      setCameraGroups([]);
      setCameraEvents([]);
    });
    const timers = [500, 1500, 3500, 7000].map(delay =>
      window.setTimeout(() => {
        void refreshCameraEvidence().catch(() => { /* retain the last good evidence */ });
      }, delay),
    );
    return () => timers.forEach(timer => window.clearTimeout(timer));
  }, [session, recheckTick, loadManifest, refreshCameraEvidence]);

  // Reset the success/error state only when a NEW session is opened, not on re-check —
  // a re-check after a successful download must not silently reset the user's ability
  // to close (it just refreshes the manifest so new rows can be downloaded again).
  useEffect(() => {
    setDownloaded(false);
    setDownloadError("");
    setPhase("idle");
    setProgressText("");
    if (!sessionKey) return;

    // The modal can receive a fresh EndSessionInfo object after a late STATE_UPDATE,
    // or be recreated after a dashboard reload. The ZIP is already durable in that
    // case, so restore the close affordance from the IndexedDB save marker instead of
    // forcing the operator to download the same recording again.
    let cancelled = false;
    isSessionSaved(sessionKey)
      .then(saved => {
        if (!cancelled && saved) setDownloaded(true);
      })
      .catch(() => { /* a missing IndexedDB record leaves the download action available */ });
    return () => { cancelled = true; };
  }, [sessionKey]);

  // ── Pull & consolidate ──────────────────────────────────────────────────
  const handleConsolidate = async () => {
    if (!session) return;
    // Consolidation is independent of browser ZIP export. Do not leave a stale ZIP
    // progress message visible while this workflow is running or after it completes.
    setDownloadProgress("");
    setPhase("waiting");
    setConsolidateResult("");
    setDataError("");
    const deadline = Date.now() + CONSOLIDATE_WAIT_MS;
    let prevSig = "";
    let unchangedStreak = 0;
    try {
      while (Date.now() < deadline) {
        const m = await waitFor(
          () => fetchManifest(backendIp, session.sessionId),
          deadline,
          seconds => setProgressText(`Waiting for phone rescue uploads… ${seconds}s left`),
        );
        setManifest(m);
        const recovery = Array.isArray(m.recovery) ? m.recovery : [];
        const recTotal = recovery.reduce((s, r) => s + (r.csv_size ?? 0), 0);
        const sig = JSON.stringify({
          late: m.late_pending,
          recTotal,
          recComplete: recovery.filter(r => r.complete).length,
        });
        if (!m.late_pending && !m.recovery_pending) break;   // nothing more expected
        if (prevSig !== "" && sig === prevSig) {
          unchangedStreak++;
          if (unchangedStreak >= 3) break;                   // stable → proceed
        } else {
          unchangedStreak = 0;
        }
        prevSig = sig;
        const secs = Math.max(0, Math.round((deadline - Date.now()) / 1000));
        setProgressText(`Waiting for phone rescue uploads… ${secs}s left`);
        await new Promise(r => setTimeout(r, POLL_MS));
      }
      setPhase("consolidating");
      setProgressText("Merging data sources (main + late + recovery)…");
      const res = await postConsolidate(backendIp, session.sessionId);
      const bySource = Object.entries(res.sources)
        .map(([k, n]) => `${k}:${n.toLocaleString()}`)
        .join(", ");
      const perRole = res.per_role
        ? Object.entries(res.per_role)
            .map(([role, st]) => `${role}: ${st.rows.toLocaleString()} rows`)
            .join(" · ")
        : "";
      setConsolidateResult(
        `Consolidated ${res.rows.toLocaleString()} rows — ${bySource} — ${res.duplicates_dropped} duplicates dropped` +
          (perRole ? ` — Per-role: ${perRole}` : ""),
      );
      setManifest(await fetchManifest(backendIp, session.sessionId));
    } catch (e) {
      setConsolidateResult("");
      setDataError(`Consolidation failed: ${e}`);
    } finally {
      setPhase("idle");
      setProgressText("");
    }
  };

  // ── Download everything as one .zip ─────────────────────────────────────
  //
  // Primary path (canStreamSave): the archive is streamed chunk-by-chunk to a real file
  // handle (see zip_stream.ts). Nothing is assembled in the JS heap, and no save signal is
  // emitted from a .click() — markSessionSaved runs only after the write handle has closed
  // successfully. The old _downloadBlob (URL + a.click + immediate revoke) is gone; it raced
  // the download and could not report failure, which is what let the only backup get deleted.

  // Count bytes as chunks stream through (cheap, bounded memory) so markSessionSaved can
  // record an accurate total after a successful close.
  const trackBytes = (sink: (c: Uint8Array | Blob) => Promise<void>, tally: { total: number }) =>
    async (chunk: Uint8Array | Blob) => {
      tally.total += chunk instanceof Blob ? chunk.size : chunk.byteLength;
      await sink(chunk);
    };

  const buildStreamEntries = async (
    m: ExportManifest | null,
    sid: string,
    tally: { total: number },
    videoReport: VideoFinalizeReport[],
  ): Promise<StreamZipEntry[]> => {
    const entries: StreamZipEntry[] = [];
    const cameraFiles = new Map<string, string[]>();
    const videoDone: Promise<void>[] = [];
    // Read the ledger at export time. The modal can render before the final IndexedDB
    // event transaction becomes visible, so a captured `cameraEvents` state value can be
    // stale even when the stop result is already safe to export.
    const exportCameraEvents = await listCameraEvents(sid);

    // Video — streamed chunk-by-chunk straight from IndexedDB, never concatenated, and
    // finalized on the way out so the saved file carries a real Duration and Cues.
    // One file per MediaRecorder run: a second run writes a second EBML header, and a
    // player stops dead at that boundary (this is what broke session 1786677865027).
    for (const r of effectiveVideoResults) {
      // Lifecycle events are not enough to detect every browser-level MediaRecorder
      // restart: a replaced track can put a second EBML header in the same recorder
      // range without a second React "started" event. Split on the actual byte headers
      // so every exported part is independently playable.
      const segments = await cameraByteSegments(sid, r.camId);
      segments.forEach((seg, i) => {
        const suffix = i === 0 ? "" : `_part${i + 1}`;
        const path = `videos/${sid}_${r.camId}_video_sync${suffix}.${_ext(r)}`;
        const files = cameraFiles.get(r.camId) ?? [];
        files.push(path);
        cameraFiles.set(r.camId, files);
        let resolveVideo: () => void = () => {};
        let rejectVideo: (error: unknown) => void = () => {};
        const done = new Promise<void>((resolve, reject) => {
          resolveVideo = resolve;
          rejectVideo = reject;
        });
        videoDone.push(done);
        entries.push({
          path,
          write: async (sink) => {
            try {
              const read = (onChunk: (b: Blob) => Promise<void>) =>
                streamCameraByteRange(sid, r.camId, seg.from, seg.to, onChunk).then(() => {});
              const res = await finalizeWebmStream(read, trackBytes(sink, tally));
              videoReport.push({ path, camId: r.camId, ...res });
              resolveVideo();
            } catch (error) {
              rejectVideo(error);
              throw error;
            }
          },
        });
      });
    }

    // Backend data artifacts (small) pulled fresh at click-time.
    if (m) {
      for (const f of m.files) {
        if (!isDataKind(f.kind)) continue;
        entries.push({
          path: `data/${f.name}`,
          write: async (sink) => {
            await streamExportFile(backendIp, sid, f.name, trackBytes(sink, tally));
          },
        });
      }
      for (const rec of m.recovery) {
        if (!rec.complete || !rec.csv_exists) continue;
        entries.push({
          path: `data/recovery/${rec.device_id}.csv`,
          write: async (sink) => {
            await streamRecoveryFile(backendIp, sid, rec.device_id, trackBytes(sink, tally));
          },
        });
      }
      entries.push({
        path: "data/manifest.json",
        write: async (sink) => {
          await trackBytes(sink, tally)(new TextEncoder().encode(JSON.stringify(m, null, 2)));
        },
      });
    } else {
      entries.push({
        path: "data/export_error.txt",
        write: async (sink) => {
          const text = `Backend data unavailable (${dataError || "not reachable"}).\n` +
            "This ZIP contains video only — pull/rescue CSVs from the Recovery screen.\n";
          await trackBytes(sink, tally)(new TextEncoder().encode(text));
        },
      });
    }

    const cameras = effectiveVideoResults.map(r => ({
      session_id: sid,
      cam_id: r.camId,
      device_id: r.deviceId,
      browser_label: r.label,
      mime: r.mime,
      // A recorder restart creates separate playable files. Keep the legacy `file`
      // field for consumers that only handle one run, while explicitly listing every
      // segment so a restarted camera is never mistaken for one stacked/corrupt file.
      file: cameraFiles.get(r.camId)?.[0] ?? `videos/${sid}_${r.camId}_video_sync.${_ext(r)}`,
      files: cameraFiles.get(r.camId) ?? [],
      started_at_ms: r.startedAtMs,
      flash_at_ms: r.flashAtMs,
      stopped_at_ms: r.stoppedAtMs,
    }));
      entries.push({
        path: "cameras.json",
        write: async (sink) => {
          await trackBytes(sink, tally)(new TextEncoder().encode(JSON.stringify({ session_id: sid, cameras, camera_events: exportCameraEvents }, null, 2)));
      },
    });
    if (missed.length > 0) {
      entries.push({
        path: "missed_cameras.txt",
        write: async (sink) => {
          await trackBytes(sink, tally)(new TextEncoder().encode(missed.join("\n") + "\n"));
        },
      });
    }
    // Written last so it observes the finalize result of every video above. This is the
    // record that says whether each file is actually seekable and holds exactly one
    // recording — the check whose absence let corrupt footage ship looking healthy.
    entries.push({
      path: "video_report.json",
      write: async (sink) => {
        // ZIP entries are fed concurrently. Wait until every video finalizer has
        // reported before serializing this evidence; otherwise the archive can contain
        // playable videos but an empty `videos` array and a misleading `healthy: true`.
        await Promise.all(videoDone);
        const payload = {
          session_id: sid,
          videos: videoReport,
          healthy: videoReport.every(v => v.ok && v.ebmlHeaders === 1),
        };
        await trackBytes(sink, tally)(new TextEncoder().encode(JSON.stringify(payload, null, 2)));
      },
    });
    return entries;
  };

  const handleDownload = async () => {
    if (!session || downloading) return;
    setDownloading(true);
    // A retry must not inherit an older success marker. Keep the chunks, but require this
    // exact export attempt to pass video validation before restoring Saved/Close.
    setDownloaded(false);
    setDownloadError("");
    setDownloadProgress("Preparing…");
    const sid = session.sessionId;
    const prefix = `${session.subject || "subject"}_${session.sessionTag || "session"}_${sid}`
      .replace(/\s+/g, "_");
    try {
      await invalidateSessionSaved(sid);
      // Fresh manifest at click-time (data artifacts are re-pulled by each entry).
      let m = manifest;
      if (!m) {
        try { m = await fetchManifest(backendIp, sid); setManifest(m); }
        catch { m = null; }
      }

      const tally = { total: 0 };
      if (canStreamSave()) {
        setDownloadProgress("Streaming videos to disk…");
      const videoReport: VideoFinalizeReport[] = [];
      // Keep the picker in the original click stack. Byte-level camera segmentation is
      // asynchronous and must happen only after Edge has granted the file handle.
      const ok = await streamZipToDisk(
        `${prefix}.zip`,
        () => buildStreamEntries(m, sid, tally, videoReport),
        setDownloadProgress,
      );
        if (!ok) {
          // User cancelled the file picker — no error, and the session is NOT marked saved.
          setDownloadProgress("");
          return;
        }
        // A successfully closed file handle only proves that bytes reached disk. The
        // finalizer also validates that every camera segment is a single WebM stream and
        // that metadata rebuilding succeeded. Keep the IndexedDB backup and the Close
        // affordance locked if the archive contains a raw/corrupt/stacked recording.
        const expectedVideos = effectiveVideoResults.length;
        const invalidVideos = videoReport.filter(v => !v.ok || v.ebmlHeaders !== 1 || v.bytesWritten <= 0);
        if ((expectedVideos > 0 && videoReport.length === 0) || invalidVideos.length > 0) {
          const details = invalidVideos
            .map(v => `${v.camId}: ${v.error ?? `${v.ebmlHeaders} EBML headers`}`)
            .join("; ");
          setDownloadError(
            `ZIP was saved, but video validation failed${details ? ` (${details})` : ""}. ` +
            "The browser backup was retained; do not delete it until the recording is recovered.",
          );
          setDownloadProgress("");
          return;
        }
        // Only here has the write handle closed successfully. This is the sole place a save
        // is confirmed — never on a click or a cancel/throw. [incident 2026-08-07]
        await markSessionSaved(sid, tally.total, true);
        setDownloaded(true);
        onDownloadComplete(sid);
      } else throw new Error("This browser cannot safely export a long recording. Open this session in Chrome or Edge.");
    } catch (e) {
      setDownloadError(`Download failed: ${e}`);
    } finally {
      setDownloading(false);
      // A completed/failed ZIP attempt must not leave its last progress label behind
      // after control returns to the end-session modal.
      setDownloadProgress("");
    }
  };

  if (!session) return null;

  const m = manifest;
  const isWhole = m?.whole ?? false;
  const status = m?.status ?? "NONE";
  const hasConsolidatedData = Boolean(
    m && Array.isArray(m.files) && m.files.some(f => f.kind === "consolidated"),
  );
  const hasPendingPhoneData = Boolean(m?.late_pending || m?.recovery_pending);
  // A PARTIAL integrity report can be expected after intentional disconnects. It must not
  // keep the operator in a perpetual "consolidate" state once the consolidated artifacts are
  // current and no phone still has data to deliver.
  const canConsolidate = Boolean(m && (!hasConsolidatedData || hasPendingPhoneData));
  const exportReady = Boolean(
    m && hasConsolidatedData && !hasPendingPhoneData && phase === "idle",
  );
  // The manifest is server-supplied JSON typed only by an interface, so a backend that
  // omits or renames a list crashes the render — at end of session, after the recording,
  // right when the operator is saving. `totalLabels` already defended itself; the JSX
  // below did not. Normalise both lists once, here.
  const labelsUsed = (Array.isArray(m?.labels_used) ? m.labels_used : []).map(l => ({
    label_id: Number(l?.label_id ?? 0),
    label_name: String(l?.label_name ?? "0"),
    row_count: Number(l?.row_count ?? 0),
  }));
  const reasons = Array.isArray(m?.reasons) ? m.reasons : [];
  const dataRows = Number(m?.data_rows ?? 0);
  const totalLabels = labelsUsed.reduce((s, l) => s + l.row_count, 0);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/80 p-4"
      onClick={e => e.stopPropagation()}
    >
      <div
        className="max-w-2xl w-full max-h-[88vh] flex flex-col glass-panel p-5 gap-3 overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between shrink-0">
          <h2 className="text-base font-bold text-gray-200 uppercase tracking-wider">
            End of Session
          </h2>
          <span className={`text-[11px] px-2 py-0.5 rounded border ${STATUS_STYLE[status] ?? STATUS_STYLE.UNKNOWN}`}>
            Integrity: {status}
          </span>
        </div>

        {/* Session stats */}
        <div className="shrink-0 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-gray-400">
          <div>Session <span className="text-gray-200 tabular-nums">{session.sessionId}</span></div>
          <div>
            Subject <span className="text-gray-200">{session.subject || "—"}</span> · Tag{" "}
            <span className="text-gray-200">{session.sessionTag || "—"}</span> · Operator{" "}
            <span className="text-gray-200">{session.operator || "—"}</span>
          </div>
          {m && <div>Data rows: <span className="text-gray-200 tabular-nums">{dataRows.toLocaleString()}</span></div>}
          <div>Videos: <span className="text-gray-200 tabular-nums">{effectiveVideoResults.length}</span>{cameraProblems.length > 0 && <span className="text-red-400"> ({cameraProblems.length} issue(s))</span>}</div>
        </div>

        {/* Labels used */}
        {m && (
          <div className="shrink-0 glass-card p-2">
            <div className="text-[11px] text-gray-400 mb-1">
              Labels used: <span className="text-cyan-300 font-bold">{labelsUsed.length}</span>{" "}
              ({totalLabels.toLocaleString()} labeled rows)
            </div>
            <div className="flex flex-wrap gap-1 max-h-16 overflow-y-auto">
              {labelsUsed.length === 0 && <span className="text-[11px] text-gray-600 italic">no labeled rows</span>}
              {labelsUsed.map(l => (
                <span
                  key={l.label_id}
                  className="text-[10px] px-1.5 py-0.5 rounded bg-accent/10 border border-accent/30 text-cyan-200 tabular-nums"
                  title={`label ${l.label_id} (${l.label_name})`}
                >
                  {String(l.label_id) === l.label_name ? l.label_id : `${l.label_id}:${l.label_name}`} × {l.row_count.toLocaleString()}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Wholeness */}
        <div className="shrink-0">
          {m?.whole && m.analysis_ready_imu !== false && cameraProblems.length === 0 ? (
            <div className="rounded-lg border border-green-500/30 bg-green-500/10 px-3 py-2 text-sm text-green-300 font-bold">
              ✓ Data considered whole
            </div>
          ) : (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm">
              <div className="text-amber-300 font-bold mb-1">Data requires review before analysis</div>
              <ul className="list-disc list-inside text-[11px] text-amber-200/90 space-y-0.5">
                {(m
                  ? reasons
                  : [dataError || "Backend finalization is still in progress; use Re-check before treating data as missing."]
                ).map((r, i) => <li key={i}>{r}</li>)}
                {m && m.analysis_ready_imu === false && <li>IMU acceptance checks failed — export remains available, but this data is not analysis-ready.</li>}
                {cameraProblems.length > 0 && <li>Camera integrity issue: {cameraProblems.join("; ")}</li>}
              </ul>
              {(m?.late_pending || m?.recovery_pending) && (
                <p className="text-[10px] text-gray-500 mt-1">
                  Phones can still flush buffered rows for up to 10 minutes after stop. Use
                  “Pull &amp; consolidate” to fetch them, or Re-check before downloading.
                </p>
              )}
            </div>
          )}
        </div>

        {/* Per-device integrity (condensed) */}
        {m?.integrity && Array.isArray((m.integrity as { devices?: unknown[] }).devices) && (
          <div className="shrink-0 grid grid-cols-1 gap-1 max-h-28 overflow-y-auto">
            {(m.integrity as { devices: Array<Record<string, unknown>> }).devices.map((d, i) => (
              <div key={i} className="flex items-center justify-between text-[11px] text-gray-400">
                <span className="text-gray-300">{String(d.role ?? (d as { device_id?: string }).device_id ?? "?")}</span>
                <span className="tabular-nums">
                  rows {Number(d.row_count ?? 0).toLocaleString()}
                  {Number(d.offline_interval_count ?? 0) > 0 &&
                    ` · ${String(d.offline_interval_count)} disconn. (${((Number(d.offline_total_ms ?? 0)) / 1000).toFixed(1)}s)`}
                  {Number(d.packets_dropped_no_writer ?? 0) > 0 &&
                    <span className="text-red-400"> · {String(d.packets_dropped_no_writer)} packets LOST</span>}
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Consolidate action */}
        {!isWhole && m && (
          <div className="shrink-0 flex items-center gap-2">
            <button
              onClick={handleConsolidate}
              disabled={phase !== "idle" || !canConsolidate}
              className="btn-glass flex-1 py-2 text-xs text-amber-200 disabled:opacity-50"
              title={!canConsolidate ? "Consolidation is current; re-check for new phone data first" : undefined}
            >
              {phase === "waiting" || phase === "consolidating"
                ? (phase === "waiting" ? "Waiting for phones…" : "Consolidating…")
                : canConsolidate
                  ? (hasConsolidatedData ? "Pull & consolidate new data" : "Pull & consolidate CSVs (incl. recovered)")
                  : "Consolidated — no pending data"}
            </button>
            <button
              onClick={loadManifest}
              disabled={phase !== "idle"}
              className="btn-glass px-3 py-2 text-xs text-gray-400 disabled:opacity-50"
              title="Re-check whether late/recovered data has arrived"
            >
              Re-check
            </button>
          </div>
        )}
        {/* Server-side save. Always available, never gated on `whole` — its whole purpose is to
            work when this dashboard cannot be relied on. The client-side zip below stays as the
            convenient path; this one is the one that survives a render crash. */}
        <div className="shrink-0">
          <button
            onClick={handleServerBundle}
            disabled={bundlePhase === "running"}
            className="btn-glass w-full py-2 text-xs text-cyan-200 disabled:opacity-50"
            title="Backend writes the data bundle to the SSD itself — does not need this browser"
          >
            {bundlePhase === "running" ? "Saving on backend…" : "💾 Save data bundle on backend (no browser needed)"}
          </button>
          {bundleResult && (
            <div className="mt-1 rounded border border-cyan-500/30 bg-cyan-500/10 px-2 py-1 text-[11px] text-cyan-200 break-all">
              {bundleResult}
            </div>
          )}
          <button
            onClick={handleDataBundleDownload}
            disabled={bundlePhase === "running"}
            className="btn-primary w-full mt-2 py-2 text-xs disabled:opacity-50"
            title="Downloads all backend CSV/artifacts even when integrity is PARTIAL or FAIL"
          >
            {bundlePhase === "running" ? "Preparing data bundle…" : "⬇ Download CSV/data bundle (even if incomplete)"}
          </button>
        </div>
        {progressText && <div className="shrink-0 text-[11px] text-gray-500">{progressText}</div>}
        {consolidateResult && (
          <div className="shrink-0 rounded border border-cyan-500/30 bg-cyan-500/10 px-2 py-1 text-[11px] text-cyan-200">
            {consolidateResult}
          </div>
        )}
        {dataError && consolidateResult === "" && (
          <div className="shrink-0 text-[11px] text-red-400">{dataError}</div>
        )}

        {/* Download */}
        <div className="shrink-0 mt-auto pt-1 border-t border-white/10 flex flex-col gap-2">
          {!canStreamSave() && (
            <div className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-300">
              This browser cannot safely export long recordings. Reopen this session in Chrome
              or Edge using the same origin and browser profile.
            </div>
          )}
          {!downloaded && (
            <p className="text-[10px] text-gray-600 text-center">
              This screen stays open until a download has completed.
            </p>
          )}
          {downloadError && <div className="text-[11px] text-red-400 text-center">{downloadError}</div>}
          {downloading && downloadProgress && <div className="text-[11px] text-gray-500 text-center">{downloadProgress}</div>}
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              disabled={downloading || !exportReady || !canStreamSave()}
              className="btn-primary flex-1 py-2 font-bold text-sm disabled:opacity-50 disabled:cursor-wait"
              title={!exportReady ? "Consolidate all phone data before downloading" : undefined}
            >
              {downloading ? "Building ZIP…" : "Download all as .zip"}
            </button>
            {downloaded && (
              <span className="text-[11px] text-green-400 whitespace-nowrap">✓ Saved</span>
            )}
            {downloaded && (
              <button
                type="button"
                onClick={event => {
                  event.preventDefault();
                  onClose();
                }}
                className="btn-success px-4 py-2 text-xs font-bold"
              >
                Close
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
