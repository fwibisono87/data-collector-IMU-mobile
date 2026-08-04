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

  const base = `http://${backendIp}:8000`;

  const refresh = useCallback(async () => {
    setError("");
    try {
      const res = await fetch(`${base}/recovery/sessions`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as RecoverySession[];
      setSessions(data.filter(s => s.files.length > 0));
    } catch (e) {
      setError(`Could not reach backend for recovery list: ${e}`);
    }
  }, [base]);

  useEffect(() => { refresh(); }, [refresh]);

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
        <button onClick={refresh} className="btn-glass text-xs text-gray-400 px-2 py-0.5">
          Refresh
        </button>
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}
      {sessions.length === 0 && !error && (
        <p className="text-xs text-gray-600 italic">
          No phone uploads yet. Phones auto-upload their local CSV after a session; check back here.
        </p>
      )}

      <div className="flex flex-col gap-2">
        {sessions.map(s => (
          <div key={s.session_id} className="rounded border border-white/10 p-2 space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold text-cyan-300 truncate">{s.session_id}</span>
              <button
                onClick={() => merge(s.session_id)}
                disabled={merging === s.session_id || s.files.some(f => !f.complete)}
                className="btn-glass text-[11px] px-2 py-0.5 disabled:opacity-30"
                title={s.files.some(f => !f.complete) ? "Wait for all phones to finish uploading" : ""}
              >
                {merging === s.session_id ? "Merging…" : "Merge"}
              </button>
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
