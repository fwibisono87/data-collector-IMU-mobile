"use client";

interface DeviceReport {
  device_id: string;
  role?: string;
  csv_path: string;
  row_count: number;
  csv_sha256: string;
  status: "PASS" | "FAIL" | "PARTIAL";
  issue?: string;
  offline_interval_count?: number;
  offline_total_ms?: number;
  rows_reordered?: number;
  packets_dropped_no_writer?: number;
}

interface Report {
  session_id: string;
  status: "PASS" | "FAIL" | "PARTIAL";
  validated_at_ms: number;
  devices: DeviceReport[];
}

const STATUS_STYLE: Record<string, string> = {
  PASS:    "text-green-400 bg-green-500/10 border-green-500/30",
  FAIL:    "text-red-400 bg-red-500/10 border-red-500/30",
  PARTIAL: "text-yellow-400 bg-yellow-500/10 border-yellow-500/30",
};

export default function IntegrityReport({ report }: { report: Report }) {
  return (
    <div className={`rounded-lg border px-4 py-3 text-sm ${STATUS_STYLE[report.status] ?? STATUS_STYLE.PARTIAL}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-bold text-base">
          Integrity: {report.status}
        </span>
        <span className="text-xs opacity-60">
          {new Date(report.validated_at_ms).toLocaleTimeString()}
        </span>
      </div>

      <p className="text-xs opacity-70 mb-2">Session: {report.session_id}</p>

      <div className="space-y-2">
        {report.devices.map(d => (
          <div key={d.device_id} className="rounded-md bg-white/5 p-2 text-xs tabular-nums space-y-0.5">
            <div className="flex justify-between">
              <span className="text-gray-300">{d.role ?? d.device_id.slice(0, 8) + "…"}</span>
              <span className={d.status === "PASS" ? "text-green-400" : "text-red-400"}>{d.status}</span>
            </div>
            <div className="text-gray-500">rows: {d.row_count.toLocaleString()}</div>
            {d.issue && <div className="text-red-400">⚠ {d.issue}</div>}
            {(d.offline_interval_count ?? 0) > 0 && (
              <div className="text-orange-400">
                ⚠ {d.offline_interval_count} disconnect(s), {((d.offline_total_ms ?? 0) / 1000).toFixed(1)} s offline
                — this is why the status is PARTIAL
              </div>
            )}
            {(d.packets_dropped_no_writer ?? 0) > 0 && (
              <div className="text-red-400">
                ✕ {(d.packets_dropped_no_writer ?? 0).toLocaleString()} packets had no open file — DATA LOST
              </div>
            )}
            {(d.rows_reordered ?? 0) > 0 && (
              <div className="text-gray-500">rows re-ordered after replay: {(d.rows_reordered ?? 0).toLocaleString()}</div>
            )}
            <div className="text-gray-600 truncate">sha256: {d.csv_sha256.slice(0, 16)}…</div>
          </div>
        ))}
      </div>
    </div>
  );
}
