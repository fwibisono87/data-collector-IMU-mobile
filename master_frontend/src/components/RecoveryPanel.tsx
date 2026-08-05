"use client";
import { useCallback, useEffect, useState } from "react";

interface RecoveryFile {
  device_id: string;
  role: string;
  subject: string;
  session_tag: string;
  size: number;
  complete: boolean;
  sha256_verified?: boolean | null;
  updated_at_ms: number;
}

interface RecoverySession {
  session_id: string;
  files: RecoveryFile[];
  done?: boolean;
}

interface Props {
  backendIp: string;
}

// "Pull from phones" — lists phone-uploaded rescue CSVs (no adb) and lets the operator
// download or merge them into the main session CSV. Data lands on the backend via the
// phone's resumable HTTP upload (see master_backend/app/upload.py).
export default function RecoveryPanel({ backendIp }: Props) {
  const [sessions, setSessions] = useState<RecoverySession[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [merging, setMerging] = useState<string>("");
  const [showDone, setShowDone] = useState(false);
  const [busyAll, setBusyAll] = useState(false);

  const base = `http://${backendIp}:8000`;

  const refresh = useCallback(async () => {
    setError("");
    try {
      const res = await fetch(`${base}/recovery/sessions${showDone ? "?include_done=1" : ""}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as RecoverySession[];
      setSessions(data.filter(s => s.files.length > 0));
    } catch (e) {
      setError(`Could not reach backend for recovery list: ${e}`);
    }
  }, [base, showDone]);

  useEffect(() => { refresh(); }, [refresh]);

  const restore = async (sid: string) => {
    setBusy(b => ({ ...b, [sid]: true }));
    setError("");
    try {
      const res = await fetch(`${base}/recovery/${encodeURIComponent(sid)}/restore`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await refresh();
    } catch (e) {
      setError(`Restore failed: ${e}`);
    } finally {
      setBusy(b => ({ ...b, [sid]: false }));
    }
  };

  // Hide every currently-listed session in one click (non-destructive; the Finished
  // toggle restores them). Only shows when there's something to clear.
  const dismissAll = async () => {
    setBusyAll(true);
    setError("");
    try {
      for (const s of sessions) {
        const res = await fetch(`${base}/recovery/${encodeURIComponent(s.session_id)}/dismiss`, { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      }
      await refresh();
    } catch (e) {
      setError(`Clear all failed: ${e}`);
    } finally {
      setBusyAll(false);
    }
  };

  const download = (sid: string, fname: string) => {
    const a = document.createElement("a");
    a.href = `${base}/recovery/${encodeURIComponent(sid)}/files/${encodeURIComponent(fname)}.csv`;
    a.download = `${sid}_${fname}.csv`;
    a.click();
  };

  const merge = async (sid: string) => {
    setMerging(sid);
    setError("");
    try {
      const res = await fetch(`${base}/recovery/${encodeURIComponent(sid)}/merge`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error((data as { detail?: string }).detail || `HTTP ${res.status}`);
      setError(`Merged into ${(data as { path: string }).path} — ${(data as { rows: number }).rows} rows`);
    } catch (e) {
      setError(`Merge failed: ${e}`);
    } finally {
      setMerging("");
    }
  };

  return (
    <div className="glass-panel p-3 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider">Recovery from phones</h3>
        <div className="flex items-center gap-2">
          {sessions.length > 0 && !showDone && (
            <button
              onClick={dismissAll}
              disabled={busyAll}
              className="btn-glass text-[11px] px-2 py-0.5 text-red-300 disabled:opacity-40"
              title="Hide all rescue files from this list (restorable via Finished)"
            >
              {busyAll ? "Clearing…" : "Clear"}
            </button>
          )}
          <label className="flex items-center gap-1 text-[10px] text-gray-500 cursor-pointer select-none" title="Show hidden/finished sessions">
            <input
              type="checkbox"
              checked={showDone}
              onChange={e => setShowDone(e.target.checked)}
              className="accent-cyan-400"
            />
            Finished
          </label>
          <button onClick={refresh} className="btn-glass text-xs text-gray-400 px-2 py-0.5">
            Refresh
          </button>
        </div>
      </div>

      {sessions.length > 0 && !showDone && (
        <p className="text-[10px] text-gray-600">
          <span className="text-red-300">Clear</span> hides these rescue files; tick
          “Finished” to see or restore them.
        </p>
      )}

      {error && <p className="text-xs text-red-400">{error}</p>}
      {sessions.length === 0 && !error && (
        <p className="text-xs text-gray-600 italic">
          No phone uploads yet. Phones auto-upload their local CSV after a session; check back here.
        </p>
      )}

      <div className="flex flex-col gap-2">
        {sessions.map(s => (
          <div key={s.session_id} className={`rounded border p-2 space-y-1 ${s.done ? "border-white/5 opacity-50" : "border-white/10"}`}>
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold text-cyan-300 truncate">{s.session_id}</span>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => merge(s.session_id)}
                  disabled={merging === s.session_id || s.done || s.files.some(f => !f.complete)}
                  className="btn-glass text-[11px] px-2 py-0.5 disabled:opacity-30"
                  title={s.done ? "Not available for hidden sessions" : (s.files.some(f => !f.complete) ? "Wait for all phones to finish uploading" : "")}
                >
                  {merging === s.session_id ? "Merging…" : "Merge"}
                </button>
                {showDone && (
                  <button
                    onClick={() => restore(s.session_id)}
                    disabled={busy[s.session_id]}
                    className="btn-glass text-[11px] px-2 py-0.5 disabled:opacity-30"
                    title="Put this session back in the main list"
                  >
                    {busy[s.session_id] ? "…" : "Restore"}
                  </button>
                )}
              </div>
            </div>
            {s.files.map(f => (
              <div key={f.device_id} className="flex items-center justify-between text-[11px]">
                <span className="text-gray-300 truncate max-w-[120px]">
                  {f.role || f.device_id}
                </span>
                <div className="flex items-center gap-2">
                  <span className="text-gray-500 tabular-nums">
                    {(f.size / 1024 / 1024).toFixed(1)} MB
                  </span>
                  <span className={f.complete ? "text-green-400" : "text-amber-400"}>
                    {f.complete ? (f.sha256_verified === false ? "bad" : "✓") : "…"}
                  </span>
                  <button
                    onClick={() => download(s.session_id, f.device_id)}
                    disabled={!f.complete}
                    className="btn-glass text-[11px] px-2 py-0.5 disabled:opacity-30"
                  >
                    Save
                  </button>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
