// IndexedDB video chunk store — survives browser crash (CLAUDE.md §9.6).
// Multi-camera: chunks are namespaced per (sessionId, camId) so N cameras never collide.
//
// Deletion policy (incident 2026-08-07): chunks are the ONLY copy of the footage until an
// operator has saved it to disk. A session's chunks may therefore be deleted only after a
// save has been *confirmed* — a completed write to a real file handle — never on the
// strength of a click. Confirmation is recorded in the `saved` store; every delete path
// consults it. The previous unconditional `clearAllChunks()` at the start of each session
// came within one click of destroying a 26-minute 3-camera session.
const DB_NAME = "imu-video-backup";
const DB_VERSION = 2;
const STORE = "chunks";
const SAVED = "saved";

interface ChunkRecord {
  key: string;
  sessionId: string;
  camId: string;
  index: number;
  blob: Blob;
}

interface SavedRecord {
  sessionId: string;
  savedAtMs: number;
  bytes: number;
}

/** One camera's footage within one session, as it currently exists on disk. */
export interface ChunkGroup {
  sessionId: string;
  camId: string;
  chunks: number;
  bytes: number;
  firstIndex: number;
  lastIndex: number;
  /** true when the chunk indices are not a complete 0..n-1 run (footage has a hole). */
  hasHole: boolean;
  /** true once this session's save has been confirmed to disk. */
  saved: boolean;
}

async function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      // Additive only — never drop `chunks`, it may hold the sole copy of a session.
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: "key" });
      }
      if (!req.result.objectStoreNames.contains(SAVED)) {
        req.result.createObjectStore(SAVED, { keyPath: "sessionId" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function chunkKey(sessionId: string, camId: string, index: number): string {
  // "__" separators: sessionId is epoch-ms digits, camId is "camN", index is an int —
  // none contain "__", so keys are unambiguous.
  return `${sessionId}__${camId}__${index}`;
}

function parseKey(key: string): { sessionId: string; camId: string; index: number } | null {
  const parts = key.split("__");
  if (parts.length !== 3) return null;
  const index = Number(parts[2]);
  if (!Number.isFinite(index)) return null;
  return { sessionId: parts[0], camId: parts[1], index };
}

export async function saveChunk(
  sessionId: string, camId: string, index: number, blob: Blob,
): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const rec: ChunkRecord = { key: chunkKey(sessionId, camId, index), sessionId, camId, index, blob };
    tx.objectStore(STORE).put(rec);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function loadChunks(sessionId: string, camId: string): Promise<Blob[]> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => {
      const all = (req.result as ChunkRecord[])
        .filter(r => r.sessionId === sessionId && r.camId === camId)
        .sort((a, b) => a.index - b.index);
      resolve(all.map(r => r.blob));
    };
    req.onerror = () => reject(req.error);
  });
}

// ── Streaming read ──────────────────────────────────────────────────────────

/**
 * Feed one camera's chunks to `onChunk` in index order, holding only ONE chunk in memory
 * at a time. This is the memory-safe replacement for `loadChunks` on the save path:
 * concatenating a 26-minute recording into a single Blob (and then an ArrayBuffer) is what
 * exhausted the renderer heap and produced "Application Error".
 *
 * Keys are fetched first and sorted numerically — lexical key order would place chunk 10
 * before chunk 2 and silently scramble the footage.
 */
export async function streamChunks(
  sessionId: string,
  camId: string,
  onChunk: (blob: Blob, index: number) => Promise<void> | void,
): Promise<number> {
  const db = await openDb();
  const keys: string[] = await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAllKeys();
    req.onsuccess = () => resolve(req.result as string[]);
    req.onerror = () => reject(req.error);
  });

  const mine = keys
    .map(k => ({ key: k, parsed: parseKey(k) }))
    .filter(x => x.parsed && x.parsed.sessionId === sessionId && x.parsed.camId === camId)
    .sort((a, b) => a.parsed!.index - b.parsed!.index);

  let n = 0;
  for (const { key, parsed } of mine) {
    const rec: ChunkRecord | undefined = await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(key);
      req.onsuccess = () => resolve(req.result as ChunkRecord | undefined);
      req.onerror = () => reject(req.error);
    });
    if (!rec) continue;
    await onChunk(rec.blob, parsed!.index);
    n++;
  }
  return n;
}

// ── Inventory ───────────────────────────────────────────────────────────────

/** Every (session, camera) group currently on disk, newest session first. */
export async function listAllChunkGroups(): Promise<ChunkGroup[]> {
  const db = await openDb();
  const all: ChunkRecord[] = await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result as ChunkRecord[]);
    req.onerror = () => reject(req.error);
  });
  const savedIds = new Set(await listSavedSessions());

  const acc = new Map<string, { g: ChunkGroup; idx: number[] }>();
  for (const r of all) {
    const k = `${r.sessionId}__${r.camId}`;
    let e = acc.get(k);
    if (!e) {
      e = {
        g: {
          sessionId: r.sessionId, camId: r.camId, chunks: 0, bytes: 0,
          firstIndex: r.index, lastIndex: r.index, hasHole: false,
          saved: savedIds.has(r.sessionId),
        },
        idx: [],
      };
      acc.set(k, e);
    }
    e.g.chunks++;
    e.g.bytes += r.blob.size;
    e.g.firstIndex = Math.min(e.g.firstIndex, r.index);
    e.g.lastIndex = Math.max(e.g.lastIndex, r.index);
    e.idx.push(r.index);
  }

  const out: ChunkGroup[] = [];
  Array.from(acc.values()).forEach(({ g, idx }) => {
    const distinct = new Set(idx).size;
    g.hasHole = distinct !== g.lastIndex - g.firstIndex + 1;
    out.push(g);
  });
  out.sort((a, b) =>
    a.sessionId === b.sessionId
      ? a.camId.localeCompare(b.camId)
      : b.sessionId.localeCompare(a.sessionId));
  return out;
}

// camId omitted → clear every camera's chunks for that session.
export async function clearChunks(sessionId: string, camId?: string): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const req = tx.objectStore(STORE).openCursor();
    req.onsuccess = () => {
      const cursor = req.result;
      if (!cursor) return;
      const v = cursor.value as ChunkRecord;
      if (v.sessionId === sessionId && (camId === undefined || v.camId === camId)) {
        cursor.delete();
      }
      cursor.continue();
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ── Save confirmation ───────────────────────────────────────────────────────

/**
 * Record that this session's footage reached disk. ONLY call this after a write handle has
 * closed successfully — not after `a.click()`, which cannot report failure.
 */
export async function markSessionSaved(sessionId: string, bytes = 0): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SAVED, "readwrite");
    const rec: SavedRecord = { sessionId, savedAtMs: Date.now(), bytes };
    tx.objectStore(SAVED).put(rec);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function isSessionSaved(sessionId: string): Promise<boolean> {
  return (await listSavedSessions()).includes(sessionId);
}

export async function listSavedSessions(): Promise<string[]> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SAVED, "readonly");
    const req = tx.objectStore(SAVED).getAll();
    req.onsuccess = () => resolve((req.result as SavedRecord[]).map(r => r.sessionId));
    req.onerror = () => reject(req.error);
  });
}

/** Sessions that still have chunks on disk and have NOT been confirmed saved. */
export async function listUnconfirmedSessions(): Promise<string[]> {
  const groups = await listAllChunkGroups();
  return Array.from(new Set(groups.filter(g => !g.saved).map(g => g.sessionId)));
}

/**
 * Reclaim space at the start of a new session WITHOUT destroying unsaved footage.
 *
 * Replaces the former `clearAllChunks()`, which wiped the store unconditionally and was the
 * single action that would have made the 2026-08-07 incident unrecoverable. Chunks are
 * dropped only for sessions with a confirmed save; everything else is retained and reported
 * so the UI can surface it.
 */
export async function clearConfirmedChunks(): Promise<{ cleared: string[]; kept: string[] }> {
  const groups = await listAllChunkGroups();
  const sessions = Array.from(new Set(groups.map(g => g.sessionId)));
  const savedIds = new Set(await listSavedSessions());

  const cleared: string[] = [];
  const kept: string[] = [];
  for (const sid of sessions) {
    if (savedIds.has(sid)) {
      await clearChunks(sid);
      cleared.push(sid);
    } else {
      kept.push(sid);
    }
  }
  return { cleared, kept };
}

// Distinct camIds that still have chunks on disk for a session (crash-recovery aid; §6).
export async function listPendingCameras(sessionId: string): Promise<string[]> {
  const groups = await listAllChunkGroups();
  return groups.filter(g => g.sessionId === sessionId).map(g => g.camId);
}

export async function hasPendingChunks(sessionId: string): Promise<boolean> {
  return (await listPendingCameras(sessionId)).length > 0;
}
