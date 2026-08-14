// Client for the backend /export router + recovery endpoints.
// These back the end-of-session export modal and are shared with RecoveryModal.

export interface ExportFile {
  name: string;
  path: string;
  size: number;
  kind: string; // main|csv|late|rescue|merged|consolidated|integrity|connectivity|late_summary|consolidation|validation|other
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
  verified?: boolean;
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
  exportable?: boolean;
  consolidated?: boolean;
  analysis_ready_imu?: boolean;
  terminal?: boolean;
  lifecycle_state?: string;
  reasons: string[];
  late_pending: boolean;
  recovery_pending: boolean;
  per_roles: string[];
  labels_used: LabelStat[];
  data_rows: number;
  integrity: Record<string, unknown> | null;
  connectivity: Record<string, unknown> | null;
  late_summary: Record<string, unknown> | null;
  validation: Record<string, unknown> | null;
  files: ExportFile[];
  recovery: RecoveryFileInfo[];
  ledger?: Record<string, unknown>;
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
  validation: Record<string, unknown>;
}

export interface RecoverySessionEntry {
  session_id: string;
  files: RecoveryFileInfo[];
  done?: boolean;
}

const DATA_KINDS = new Set([
  "main", "csv", "late", "rescue", "merged", "consolidated",
  "integrity", "connectivity", "late_summary", "consolidation",
  "consolidated_validation", "timing",
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

async function streamResponse(
  res: Response,
  onChunk: (chunk: Uint8Array) => Promise<void> | void,
): Promise<void> {
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  if (!res.body) throw new Error("backend response has no streaming body");
  const reader = res.body.getReader();
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) return;
      await onChunk(part.value);
    }
  } finally {
    reader.releaseLock();
  }
}

/** Stream a backend artifact without materialising a long CSV in the renderer heap. */
export async function streamExportFile(
  ip: string,
  sessionId: string,
  name: string,
  onChunk: (chunk: Uint8Array) => Promise<void> | void,
): Promise<void> {
  await streamResponse(
    await fetch(`${base(ip)}/export/${encodeURIComponent(sessionId)}/file?name=${encodeURIComponent(name)}`),
    onChunk,
  );
}

export async function postConsolidate(ip: string, sessionId: string): Promise<ConsolidateResult> {
  return _json<ConsolidateResult>(
    await fetch(`${base(ip)}/export/${encodeURIComponent(sessionId)}/consolidate`, {
      method: "POST",
    }),
  );
}

export interface BundleResult {
  session_id: string;
  path: string;
  size: number;
  entries: string[];
  contains_video: boolean;
}

/**
 * Ask the backend to write the session's data bundle to the SSD itself.
 *
 * The zip this dashboard builds with jszip is convenient but makes the browser load-bearing for
 * the deliverable: on 2026-08-11 a render crash at save left the operator with nothing to hand
 * over even though the backend had finalised perfectly. This path does not need the browser to
 * survive, and works with the dashboard closed or on another machine.
 */
export async function postBundle(ip: string, sessionId: string): Promise<BundleResult> {
  return _json<BundleResult>(
    await fetch(`${base(ip)}/export/${encodeURIComponent(sessionId)}/bundle`, {
      method: "POST",
    }),
  );
}

export function dataBundleUrl(ip: string, sessionId: string): string {
  return `${base(ip)}/export/${encodeURIComponent(sessionId)}/bundle/file`;
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

export async function streamRecoveryFile(
  ip: string,
  sessionId: string,
  deviceId: string,
  onChunk: (chunk: Uint8Array) => Promise<void> | void,
): Promise<void> {
  await streamResponse(
    await fetch(`${base(ip)}/recovery/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(deviceId)}.csv`),
    onChunk,
  );
}

export function isDataKind(kind: string): boolean {
  return DATA_KINDS.has(kind);
}
