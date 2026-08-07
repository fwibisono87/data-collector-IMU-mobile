"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  clearChunks,
  listAllChunkGroups,
  markSessionSaved,
  streamChunks,
  type ChunkGroup,
} from "@/lib/video_backup";

interface Props {
  open: boolean;
  onClose: () => void;
}

interface RowBusy {
  type: "save" | "saveAll" | "delete";
  label: string;
}

const wait = (ms: number) => new Promise<void>(r => setTimeout(r, ms));

interface SaveFilePickerHandle {
  createWritable(): Promise<{ write(blob: Blob): Promise<void>; close(): Promise<void>; abort(): Promise<void> }>;
  suggestedName?: string;
}
declare global {
  interface Window {
    showSaveFilePicker?: (opts?: {
      suggestedName?: string;
      types?: { description: string; accept: Record<string, string[]> }[];
    }) => Promise<SaveFilePickerHandle>;
  }
}

// Per-camera save. Uses the File System Access API (showSaveFilePicker) when present so the
// written data is confirmed to disk via a real handle; otherwise falls back to an in-memory
// Blob + anchor download, which is memory-bound and cannot confirm the write, so it never
// marks the session saved.
async function saveOneCamera(
  group: ChunkGroup,
  onProgress: (done: number, total: number) => void,
): Promise<{ confirmed: boolean }> {
  const total = group.chunks;
  const filename = `${group.sessionId}_${group.camId}_RESCUED.webm`;

  if (typeof window.showSaveFilePicker === "function") {
    const handle = await window.showSaveFilePicker({
      suggestedName: filename,
      types: [{ description: "WebM video", accept: { "video/webm": [".webm"] } }],
    });
    const writable = await handle.createWritable();
    try {
      let done = 0;
      await streamChunks(group.sessionId, group.camId, async blob => {
        await writable.write(blob);
        done += 1;
        onProgress(done, total);
      });
      await writable.close();
    } catch (e) {
      try { await writable.abort(); } catch { /* ignore */ }
      throw e;
    }
    // The handle closed cleanly — the bytes are on disk. This is the ONLY path that may
    // report a confirmed save.
    return { confirmed: true };
  }

  // Fallback: no picker available — build a Blob and anchor download. Memory-bound, cannot
  // report a confirmed write, and the object URL is revoked only after 15 s (never
  // synchronously after .click()).
  const parts: Blob[] = [];
  let done = 0;
  await streamChunks(group.sessionId, group.camId, async blob => {
    parts.push(blob);
    done += 1;
    onProgress(done, total);
  });
  const url = URL.createObjectURL(new Blob(parts, { type: "video/webm" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  await wait(15000);
  URL.revokeObjectURL(url);
  // An anchor download cannot report whether the bytes ever reached disk. Treating this as
  // success is exactly the 2026-08-07 defect — it let clearChunks run against footage that
  // was never written. Never confirmed on this path.
  return { confirmed: false };
}

export default function VideoRecoveryModal({ open, onClose }: Props) {
  const [groups, setGroups] = useState<ChunkGroup[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Record<string, RowBusy | undefined>>({});
  const [progress, setProgress] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState<Record<string, boolean>>({});
  const [pickedFallback, setPickedFallback] = useState(false);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const data = await listAllChunkGroups();
      setGroups(data);
      setPickedFallback(typeof window !== "undefined" && typeof window.showSaveFilePicker !== "function");
    } catch (e) {
      setError(`Could not read buffered footage: ${e}`);
    }
  }, []);

  useEffect(() => { if (open) refresh(); }, [open, refresh]);

  const sessions = useMemo(() => {
    const by = new Map<string, ChunkGroup[]>();
    for (const g of groups) {
      const arr = by.get(g.sessionId) ?? [];
      arr.push(g);
      by.set(g.sessionId, arr);
    }
    return Array.from(by.entries()).map(([sessionId, cams]) => ({
      sessionId,
      cams: cams.slice().sort((a, b) => a.camId.localeCompare(b.camId)),
    }));
  }, [groups]);

  const anySaved = sessions.some(s => s.cams.some(c => c.saved));

  const totals = useMemo(() => {
    const bytes = groups.reduce((sum, g) => sum + g.bytes, 0);
    return { bytes };
  }, [groups]);

  const keyOf = (sessionId: string, camId: string) => `${sessionId}__${camId}`;
  const busyKey = (sessionId: string) => `${sessionId}__all`;

  const mark = (sessionId: string, camId?: string, busyRow?: RowBusy) => {
    const k = camId ? keyOf(sessionId, camId) : busyKey(sessionId);
    setBusy(b => (busyRow === undefined ? { ...b, [k]: undefined } : { ...b, [k]: busyRow }));
    if (!camId) setProgress(p => ({ ...p, [busyKey(sessionId)]: "" }));
  };

  const handleSaveCamera = async (sessionId: string, camId: string) => {
    const group = groups.find(g => g.sessionId === sessionId && g.camId === camId);
    if (!group) return;
    const k = keyOf(sessionId, camId);
    const busyRow: RowBusy = { type: "save", label: "Saving…" };
    mark(sessionId, camId, busyRow);
    setError("");
    setProgress(p => ({ ...p, [k]: "" }));
    try {
      await saveOneCamera(group, (done, total) =>
        setProgress(p => ({ ...p, [k]: `saving chunk ${done}/${total}…` })));
      setProgress(p => ({ ...p, [k]: "" }));
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === "AbortError";
      setProgress(p => ({ ...p, [k]: "" }));
      if (!aborted) setError(`Failed to save ${camId}: ${e}`);
    } finally {
      mark(sessionId, camId, undefined);
    }
  };

  const handleSaveAll = async (sessionId: string) => {
    const cams = sessions.find(s => s.sessionId === sessionId)?.cams ?? [];
    if (cams.length === 0) return;
    const k = busyKey(sessionId);
    mark(sessionId, undefined, { type: "saveAll", label: "Saving…" });
    setError("");
    setProgress(p => ({ ...p, [k]: "" }));
    const savedCams = new Map<string, number>();
    let totalBytes = 0;
    let aborted = false;
    let failed = false;
    let allConfirmed = true;
    try {
      for (const cam of cams) {
        const camKey = keyOf(sessionId, cam.camId);
        let confirmed = false;
        const runErr = await new Promise<unknown>(resolve => {
          saveOneCamera(cam, (done, total) =>
            setProgress(p => ({ ...p, [camKey]: `saving chunk ${done}/${total}…` })))
            .then(r => { confirmed = r.confirmed; resolve(null); })
            .catch(e => resolve(e));
        });
        setProgress(p => ({ ...p, [camKey]: "" }));
        if (runErr) {
          if (runErr instanceof DOMException && runErr.name === "AbortError") {
            aborted = true;
          } else {
            failed = true;
            setError(`Failed to save ${cam.camId}: ${runErr}`);
          }
          break;
        }
        if (!confirmed) allConfirmed = false;
        savedCams.set(cam.camId, cam.bytes);
        totalBytes += cam.bytes;
      }
    } finally {
      mark(sessionId, undefined, undefined);
    }

    // Mark saved only when EVERY camera wrote AND every write was CONFIRMED through a real
    // file handle. The anchor-download fallback cannot confirm delivery, so it must never
    // unlock deletion — marking on an unverifiable signal is the defect this whole change
    // exists to remove. `failed` is a local flag because the `error` state read here would
    // be a stale closure from the render that created this handler.
    if (!aborted && !failed && allConfirmed && savedCams.size === cams.length) {
      try {
        await markSessionSaved(sessionId, totalBytes);
        await refresh();
      } catch (e) {
        setError(`Could not mark ${sessionId} saved: ${e}`);
      }
    } else {
      if (!aborted && !failed && !allConfirmed) {
        setError(
          `Saved ${sessionId} via the fallback downloader, which cannot confirm the write. ` +
          `Footage is retained and stays deletable only after a confirmed save.`,
        );
      }
      setProgress(p => ({ ...p, [k]: aborted ? "cancelled" : "" }));
      await refresh();
    }
  };

  const handleDelete = async (sessionId: string) => {
    if (!confirmDelete[sessionId]) {
      setConfirmDelete(c => ({ ...c, [sessionId]: true }));
      return;
    }
    mark(sessionId, undefined, { type: "delete", label: "Deleting…" });
    setError("");
    try {
      await clearChunks(sessionId);
      setConfirmDelete(c => ({ ...c, [sessionId]: false }));
      await refresh();
    } catch (e) {
      setError(`Delete failed: ${e}`);
      mark(sessionId, undefined, undefined);
    }
  };

  const cancelConfirm = (sessionId: string) => setConfirmDelete(c => ({ ...c, [sessionId]: false }));

  if (!open) return null;

  const mb = (bytes: number) => (bytes / 1024 / 1024).toFixed(1);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="max-w-3xl w-full max-h-[85vh] flex flex-col glass-panel p-4 gap-3"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between shrink-0">
          <h2 className="text-sm font-bold text-gray-300 uppercase tracking-wider">Video recovery</h2>
          <div className="flex items-center gap-2">
            <button onClick={refresh} className="btn-glass text-xs text-gray-400 px-2 py-1">
              Refresh
            </button>
            <button onClick={onClose} className="btn-glass text-xs px-2 py-1 text-gray-300">
              Close
            </button>
          </div>
        </div>

        <div className="shrink-0 text-[11px] text-gray-600">
          Buffered webcam footage still on this browser&apos;s disk. Footage survives a crash but is
          only reachable here. Marking a session saved is the only thing that permits later deletion.
        </div>

        {pickedFallback && (
          <p className="shrink-0 text-[11px] text-amber-400">
            File picker unavailable — saving uses an in-memory Blob anchor download (memory-bound) and
            cannot confirm the write, so it will not mark the session saved.
          </p>
        )}

        <div className="shrink-0 text-[11px] text-gray-400 tabular-nums">
          {sessions.length} session{sessions.length === 1 ? "" : "s"},{" "}
          {groups.length} camera{groups.length === 1 ? "" : "s"}, {mb(totals.bytes)} MB on disk
        </div>

        {error && <p className="text-xs text-red-400 shrink-0">{error}</p>}

        {sessions.length === 0 && !error && (
          <div className="shrink-0 space-y-1">
            <p className="text-xs text-gray-600 italic">No footage buffered in this browser.</p>
            <p className="text-[11px] text-gray-600">
              IndexedDB is scoped per origin AND per browser profile — footage recorded at
              localhost:3000 will not appear when the page is opened via a LAN IP.
            </p>
          </div>
        )}

        {sessions.length > 0 && (
          <div className="flex-1 overflow-y-auto min-h-0 flex flex-col gap-3">
            {sessions.map(s => {
              const sessionBusy = busy[busyKey(s.sessionId)];
              const anyCamBusy = s.cams.some(c => busy[keyOf(s.sessionId, c.camId)]);
              const totalBytes = s.cams.reduce((sum, c) => sum + c.bytes, 0);
              const allSaved = s.cams.length > 0 && s.cams.every(c => c.saved);
              return (
                <div key={s.sessionId} className="rounded border border-white/10 p-2 space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-bold text-cyan-300 truncate max-w-[220px]">
                      {s.sessionId}
                    </span>
                    <span className={`text-[11px] tabular-nums ${allSaved ? "text-green-400" : "text-amber-400"}`}>
                      {allSaved ? "saved" : "unsaved"} · {mb(totalBytes)} MB
                    </span>
                  </div>

                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      onClick={() => handleSaveAll(s.sessionId)}
                      disabled={!!sessionBusy || anyCamBusy}
                      className="btn-primary text-[11px] px-2 py-1 disabled:opacity-30"
                    >
                      {sessionBusy?.type === "saveAll" ? sessionBusy.label : "Save all for session"}
                    </button>
                    <button
                      onClick={() => handleDelete(s.sessionId)}
                      disabled={!allSaved || !!sessionBusy || anyCamBusy}
                      className={`btn-danger text-[11px] px-2 py-1 disabled:opacity-30 ${confirmDelete[s.sessionId] ? "ring-1 ring-red-400" : ""}`}
                      title={allSaved ? "" : "Only sessions confirmed saved can be deleted"}
                    >
                      {confirmDelete[s.sessionId]
                        ? sessionBusy?.type === "delete"
                          ? "Deleting…"
                          : "Confirm delete?"
                        : "Delete"}
                    </button>
                    {confirmDelete[s.sessionId] && !sessionBusy && (
                      <button
                        onClick={() => cancelConfirm(s.sessionId)}
                        className="btn-glass text-[11px] px-2 py-1 text-gray-300"
                      >
                        Cancel
                      </button>
                    )}
                  </div>

                  {sessionBusy && (
                    <div className="text-[11px] text-gray-400 tabular-nums">
                      {s.cams.map(c => {
                        const pk = keyOf(s.sessionId, c.camId);
                        const p = progress[pk];
                        return (
                          <span key={c.camId} className="mr-3 text-cyan-300">
                            {c.camId}: {p || "saving…"}
                          </span>
                        );
                      })}
                    </div>
                  )}

                  <div className="flex items-center justify-between text-[11px] text-gray-500">
                    <span className="text-gray-500">cameras</span>
                    <span className="text-gray-500 tabular-nums">{mb(totalBytes)} MB</span>
                  </div>

                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="text-gray-600 text-left">
                        <th className="py-1 font-medium">Camera</th>
                        <th className="py-1 font-medium">Chunks</th>
                        <th className="py-1 font-medium">Size</th>
                        <th className="py-1 font-medium">≈min</th>
                        <th className="py-1 font-medium">Status</th>
                        <th className="py-1 font-medium text-right">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {s.cams.map(c => {
                        const ck = keyOf(s.sessionId, c.camId);
                        const rowBusy = busy[ck];
                        const rowProgress = progress[ck];
                        return (
                          <tr key={c.camId} className="border-t border-white/5">
                            <td className="py-1 text-cyan-300 font-bold">{c.camId}</td>
                            <td className="py-1 text-gray-400 tabular-nums">{c.chunks}</td>
                            <td className="py-1 text-gray-300 tabular-nums">{mb(c.bytes)} MB</td>
                            <td className="py-1 text-gray-400 tabular-nums">{(c.chunks / 60).toFixed(1)}</td>
                            <td className="py-1">
                              <span className={c.saved ? "text-green-400" : "text-amber-400"}>
                                {c.saved ? "saved" : "unsaved"}
                              </span>
                            </td>
                            <td className="py-1 text-right">
                              <button
                                onClick={() => handleSaveCamera(s.sessionId, c.camId)}
                                disabled={!!rowBusy || !!sessionBusy || !!anyCamBusy}
                                className="btn-glass text-[11px] px-2 py-0.5 disabled:opacity-30"
                              >
                                {rowBusy ? rowBusy.label : "Save one camera"}
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>

                  {s.cams
                    .filter(c => c.hasHole)
                    .map(c => (
                      <p key={c.camId} className="text-[11px] text-amber-400">
                        {c.camId}: footage has a gap at the chunk-index level and may be truncated or corrupt.
                      </p>
                    ))}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
