// Client for the backend /export router + recovery endpoints.
// These back the end-of-session export modal and are shared with RecoveryModal.

export interface ExportFile {
  name: string;
  path: string;
  size: number;
  kind: string; // main|csv|late|rescue|merged|consolidated|integrity|connectivity|late_summary|consolidation|other
  folder: string;
}

export interface RecoveryFileInfo {
  session_id: string;
  device_id: string;
  role: string;
  subject: string;
  session_tag: string;
  operator: string;
  complete: boolean;
  done: boolean;
  received_bytes: number;
  total_bytes: number;
  sha256?: string;
  sha256_verified?: boolean | null;
  csv_exists: boolean;
  csv_size: number;
  csv_path: string;
  size?: number;                     // present on /recovery/{sid}/files entries
}

export interface LabelStat {
  label_id: number;
  label_name: string;
  row_count: number;
}

export interface ExportManifest {
  session_id: string;
  found: boolean;
  subject: string;
  session_tag: string;
  operator: string;
  status: string; // PASS|PARTIAL|FAIL|UNKNOWN|NONE
  whole: boolean;
  reasons: string[];
  late_pending: boolean;
  recovery_pending: boolean;
  per_roles: string[];
  labels_used: LabelStat[];
  data_rows: number;
  integrity: Record<string, unknown> | null;
  connectivity: Record<string, unknown> | null;
  late_summary: Record<string, unknown> | null;
  files: ExportFile[];
  recovery: RecoveryFileInfo[];
}

export interface PerRoleStat {
  path: string;
  rows: number;
  sources: Record<string, number>;
  duplicates_dropped: number;
}

export interface ConsolidateResult {
  session_id: string;
  path: string;
  rows: number;
  sources: Record<string, number>;
  duplicates_dropped: number;
  per_role: Record<string, PerRoleStat>;
}

export interface RecoverySessionEntry {
  session_id: string;
  files: RecoveryFileInfo[];
  done?: boolean;
}

const DATA_KINDS = new Set([
  "main", "csv", "late", "rescue", "merged", "consolidated",
  "integrity", "connectivity", "late_summary", "consolidation",
]);

function base(ip: string): string {
  return `http://${ip}:8000`;
}

async function _json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

// ── Export endpoints ───────────────────────────────────────────────────────

export async function fetchManifest(ip: string, sessionId: string): Promise<ExportManifest> {
  return _json<ExportManifest>(
    await fetch(`${base(ip)}/export/${encodeURIComponent(sessionId)}/manifest`),
  );
}

export async function fetchExportFile(
  ip: string, sessionId: string, name: string,
): Promise<Blob> {
  const res = await fetch(
    `${base(ip)}/export/${encodeURIComponent(sessionId)}/file?name=${encodeURIComponent(name)}`,
  );
  if (!res.ok) throw new Error(`fetch export file ${name}: HTTP ${res.status}`);
  return res.blob();
}

export async function postConsolidate(ip: string, sessionId: string): Promise<ConsolidateResult> {
  return _json<ConsolidateResult>(
    await fetch(`${base(ip)}/export/${encodeURIComponent(sessionId)}/consolidate`, {
      method: "POST",
    }),
  );
}

// ── Recovery endpoints (shared with RecoveryModal) ─────────────────────────

export async function fetchRecoverySessions(
  ip: string, includeDone = false,
): Promise<RecoverySessionEntry[]> {
  const q = includeDone ? "?include_done=1" : "";
  const sessions = await _json<RecoverySessionEntry[]>(
    await fetch(`${base(ip)}/recovery/sessions${q}`),
  );
  return sessions.filter(s => s.files.length > 0);
}

export async function fetchRecoveryFile(
  ip: string, sessionId: string, deviceId: string,
): Promise<Blob> {
  const res = await fetch(
    `${base(ip)}/recovery/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(deviceId)}.csv`,
  );
  if (!res.ok) throw new Error(`fetch recovery file ${deviceId}: HTTP ${res.status}`);
  return res.blob();
}

export function isDataKind(kind: string): boolean {
  return DATA_KINDS.has(kind);
}
