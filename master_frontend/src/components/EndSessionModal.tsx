"use client";
import { useCallback, useEffect, useState } from "react";
import JSZip from "jszip";
import {
  fetchManifest,
  fetchExportFile,
  postConsolidate,
  fetchRecoveryFile,
  isDataKind,
  type ExportManifest,
} from "@/lib/export_client";

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
  blob: Blob;
  mime: string;
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

function _ext(r: EndSessionVideoResult): string {
  return r.mime.includes("mp4") ? "mp4" : "webm";
}

function _downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
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

  const loadManifest = useCallback(async () => {
    if (!session) return;
    setDataError("");
    setConsolidateResult("");
    try {
      setManifest(await fetchManifest(backendIp, session.sessionId));
    } catch (e) {
      setManifest(null);
      setDataError(`Could not read session data from backend: ${e}`);
    }
  }, [session, backendIp]);

  // (Re)load whenever opened or externally poked (late delivery arrived).
  useEffect(() => {
    if (!session) return;
    loadManifest();
  }, [session, recheckTick, loadManifest]);

  // Reset the success/error state only when a NEW session is opened, not on re-check —
  // a re-check after a successful download must not silently reset the user's ability
  // to close (it just refreshes the manifest so new rows can be downloaded again).
  useEffect(() => {
    setDownloaded(false);
    setDownloadError("");
    setPhase("idle");
    setProgressText("");
  }, [session]);

  // ── Pull & consolidate ──────────────────────────────────────────────────
  const handleConsolidate = async () => {
    if (!session) return;
    setPhase("waiting");
    setConsolidateResult("");
    setDataError("");
    const deadline = Date.now() + CONSOLIDATE_WAIT_MS;
    let prevSig = "";
    let unchangedStreak = 0;
    try {
      while (Date.now() < deadline) {
        const m = await fetchManifest(backendIp, session.sessionId);
        setManifest(m);
        const recTotal = m.recovery.reduce((s, r) => s + (r.csv_size ?? 0), 0);
        const sig = JSON.stringify({
          late: m.late_pending,
          recTotal,
          recComplete: m.recovery.filter(r => r.complete).length,
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
  const handleDownload = async () => {
    if (!session || downloading) return;
    setDownloading(true);
    setDownloadError("");
    setDownloadProgress("Preparing…");
    const sid = session.sessionId;
    const prefix = `${session.subject || "subject"}_${session.sessionTag || "session"}_${sid}`
      .replace(/\s+/g, "_");
    try {
      const zip = new JSZip();

      // Videos live in browser memory after recording stops.
      const videos = zip.folder("videos")!;
      for (const r of videoResults) {
        // Add as ArrayBuffer — universally supported by JSZip and avoids any
        // Blob-vs-streaming quirks when streamFiles is enabled.
        videos.file(`${sid}_${r.camId}_video_sync.${_ext(r)}`, await r.blob.arrayBuffer());
        setDownloadProgress(`Adding ${r.camId}…`);
        await new Promise(res => setTimeout(res, 0));   // keep UI responsive
      }

      // Data artifacts pulled fresh from the backend at click-time.
      let m = manifest;
      if (!m) {
        try { m = await fetchManifest(backendIp, sid); setManifest(m); }
        catch { m = null; }
      }
      const data = zip.folder("data")!;
      if (m) {
        for (const f of m.files) {
          if (!isDataKind(f.kind)) continue;
          const buf = await (await fetchExportFile(backendIp, sid, f.name)).arrayBuffer();
          data.file(f.name, buf);
        }
        const rec = data.folder("recovery")!;
        for (const r of m.recovery) {
          if (!r.complete || !r.csv_exists) continue;
          const buf = await (await fetchRecoveryFile(backendIp, sid, r.device_id)).arrayBuffer();
          rec.file(`${r.device_id}.csv`, buf);
        }
        data.file("manifest.json", JSON.stringify(m, null, 2));
      } else {
        data.file("export_error.txt",
          `Backend data unavailable (${dataError || "not reachable"}).\n` +
          "This ZIP contains video only — pull/rescue CSVs from the Recovery screen.\n");
      }

      const cameras = videoResults.map(r => ({
        session_id: sid,
        cam_id: r.camId,
        device_id: r.deviceId,
        browser_label: r.label,
        mime: r.mime,
        file: `videos/${sid}_${r.camId}_video_sync.${_ext(r)}`,
      }));
      zip.file("cameras.json", JSON.stringify({ session_id: sid, cameras }, null, 2));
      if (missed.length > 0) zip.file("missed_cameras.txt", missed.join("\n") + "\n");

      const blob = await zip.generateAsync(
        { type: "blob", compression: "STORE", streamFiles: true },
        meta => setDownloadProgress(`Compressing… ${Math.round(meta.percent)}%`),
      );
      _downloadBlob(blob, `${prefix}.zip`);
      setDownloadProgress("");
      setDownloaded(true);
      onDownloadComplete(sid);
    } catch (e) {
      setDownloadError(`Download failed: ${e}`);
    } finally {
      setDownloading(false);
    }
  };

  if (!session) return null;

  const m = manifest;
  const isWhole = m?.whole ?? false;
  const status = m?.status ?? "NONE";
  const totalLabels = (m?.labels_used ?? []).reduce((s, l) => s + l.row_count, 0);

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
          {m && <div>Data rows: <span className="text-gray-200 tabular-nums">{m.data_rows.toLocaleString()}</span></div>}
          <div>Videos: <span className="text-gray-200 tabular-nums">{videoResults.length}</span>{missed.length > 0 && <span className="text-red-400"> ({missed.length} missed)</span>}</div>
        </div>

        {/* Labels used */}
        {m && (
          <div className="shrink-0 glass-card p-2">
            <div className="text-[11px] text-gray-400 mb-1">
              Labels used: <span className="text-cyan-300 font-bold">{m.labels_used.length}</span>{" "}
              ({totalLabels.toLocaleString()} labeled rows)
            </div>
            <div className="flex flex-wrap gap-1 max-h-16 overflow-y-auto">
              {m.labels_used.length === 0 && <span className="text-[11px] text-gray-600 italic">no labeled rows</span>}
              {m.labels_used.map(l => (
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
          {m?.whole ? (
            <div className="rounded-lg border border-green-500/30 bg-green-500/10 px-3 py-2 text-sm text-green-300 font-bold">
              ✓ Data considered whole
            </div>
          ) : (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm">
              <div className="text-amber-300 font-bold mb-1">Data not yet whole</div>
              <ul className="list-disc list-inside text-[11px] text-amber-200/90 space-y-0.5">
                {(m ? m.reasons : ["session data not found on backend"]).map((r, i) => <li key={i}>{r}</li>)}
                {!m && dataError && <li>{dataError}</li>}
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
              disabled={phase !== "idle"}
              className="btn-glass flex-1 py-2 text-xs text-amber-200 disabled:opacity-50"
            >
              {phase === "waiting" || phase === "consolidating"
                ? (phase === "waiting" ? "Waiting for phones…" : "Consolidating…")
                : "Pull & consolidate CSVs (incl. recovered)"}
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
          {!downloaded && (
            <p className="text-[10px] text-gray-600 text-center">
              This screen stays open until a download has completed.
            </p>
          )}
          {downloadError && <div className="text-[11px] text-red-400 text-center">{downloadError}</div>}
          {downloadProgress && <div className="text-[11px] text-gray-500 text-center">{downloadProgress}</div>}
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              disabled={downloading}
              className="btn-primary flex-1 py-2 font-bold text-sm disabled:opacity-50 disabled:cursor-wait"
            >
              {downloading ? "Building ZIP…" : "Download all as .zip"}
            </button>
            {downloaded && (
              <span className="text-[11px] text-green-400 whitespace-nowrap">✓ Saved</span>
            )}
            {downloaded && (
              <button onClick={onClose} className="btn-success px-4 py-2 text-xs font-bold">
                Close
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
