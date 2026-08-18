// Streaming .zip save to disk via the File System Access API.
//
// WHY this exists (incident 2026-08-07): the previous end-of-session export built the whole
// archive in the JS heap with zip.generateAsync() and then _downloadBlob() — two full copies
// of the entire recording in memory — which exhausted the renderer and produced
// "Application error". Worse, the "download complete" signal came from a.click(), which
// cannot report failure, so the sole backup was deleted on a signal that was never verified.
//
// This module streams the archive to a real file handle instead: JSZip's internal stream
// emits the compressed (STORE) archive chunk-by-chunk and each chunk is written through to
// the disk file before the next is read. Backpressure (pause/resume + awaiting each write)
// keeps the heap flat regardless of session length, and save confirmation is only reported
// after writable.close() resolves.
//
// JSZip's file() accepts a stream as file data (it duck-types .on/.pause/.resume, so no extra
// dependency is needed). Each camera's chunks are fed one at a time through the entry's
// `write` callback — never concatenated into a single Blob/ArrayBuffer.
"use client";

import JSZip from "jszip";

// File System Access typings live in src/types/file_system_access.d.ts — shared with
// VideoRecoveryModal, which needs the same API. Declaring them per-module produced
// conflicting `declare global` blocks that only surfaced once both were merged.

export function canStreamSave(): boolean {
  return typeof window !== "undefined" && "showSaveFilePicker" in window;
}

export interface StreamZipEntry {
  path: string;
  write: (sink: (chunk: Uint8Array | Blob) => Promise<void>) => Promise<void>;
}

export type StreamZipEntries = StreamZipEntry[] | (() => Promise<StreamZipEntry[]>);

// JSZip's NodejsStreamInputAdapter consumes any object with .on/.pause/.resume and emits the
// raw chunk via the "data" event. We implement the smallest such object ourselves so there is
// no dependency on Node's stream module (unavailable in the browser bundle) or on JSZip's
// private GenericWorker. Backpressure is provided by a small buffer: pushChunk() stalls while
// the buffer is full, and pause()/resume() gate emission exactly like a real readable.
interface SourceStream {
  on(event: "data" | "end" | "error", listener: (...args: never[]) => void): SourceStream;
  pause(): this;
  resume(): this;
}

interface SourceHandle {
  stream: SourceStream;
  pushChunk(chunk: Uint8Array): Promise<void>;
  endStream(): void;
  fail(err: unknown): void;
}

const SOURCE_BUFFER_MAX = 512 * 1024;

function createSource(): SourceHandle {
  const listeners: {
    data: Array<(chunk: Uint8Array) => void>;
    end: Array<() => void>;
    error: Array<(err: unknown) => void>;
  } = { data: [], end: [], error: [] };
  const buffer: Uint8Array[] = [];
  let paused = false;
  let ended = false;
  let errored: unknown = null;
  let bytesInBuffer = 0;

  function flush() {
    while (!paused && buffer.length > 0 && !errored) {
      const chunk = buffer.shift()!;
      bytesInBuffer -= chunk.byteLength;
      for (const cb of listeners.data) cb(chunk);
    }
    if (!paused && buffer.length === 0 && ended && !errored) {
      for (const cb of listeners.end) cb();
    }
  }

  function emitError(err: unknown) {
    errored = err;
    for (const cb of listeners.error) cb(err);
  }

  const stream: SourceStream = {
    on(event, listener) {
      listeners[event].push(listener as never);
      return stream;
    },
    pause() {
      paused = true;
      return this;
    },
    resume() {
      paused = false;
      flush();
      return this;
    },
  };

  return {
    stream,
    async pushChunk(chunk) {
      if (ended) return;
      if (errored) return;
      while (bytesInBuffer >= SOURCE_BUFFER_MAX && !ended && !errored) {
        // Wait for the consumer to drain before buffering more — this is what bounds memory.
        // `errored` is the escape hatch: without it, a consumer that stops draining because
        // the disk write failed left this loop spinning on a zero-delay timer forever,
        // pinning the tab at the moment the operator most needed to retry.
        await new Promise<void>((resolve) => setTimeout(resolve, 0));
      }
      if (ended || errored) return;
      buffer.push(chunk);
      bytesInBuffer += chunk.byteLength;
      flush();
    },
    endStream() {
      ended = true;
      flush();
    },
    fail(err) {
      emitError(err);
    },
  };
}

async function runStreamToDisk(
  entries: StreamZipEntry[],
  writable: FileSystemWritableFileStreamLike,
  onProgress?: (msg: string) => void,
): Promise<void> {
  const zip = new JSZip();
  const sources: SourceHandle[] = [];
  const feeds: Promise<void>[] = [];
  const feedErrors: unknown[] = [];

  for (const entry of entries) {
    const source = createSource();
    sources.push(source);
    zip.file(entry.path, source.stream as unknown as Uint8Array);

    feeds.push(
      entry
        .write(async (raw) => {
          // Normalize to a Uint8Array so the zip pipeline sees binary bytes (no Blob concat).
          const chunk = raw instanceof Blob
            ? new Uint8Array(await raw.arrayBuffer())
            : raw;
          await source.pushChunk(chunk);
        })
        .then(() => { source.endStream(); })
        // Fail the member rather than ending it. endStream() let JSZip finish the entry
        // with a correct CRC over truncated content, producing a structurally valid
        // archive containing a silently short CSV or video. The error is recorded here
        // instead of rethrown so this promise can never become an unhandled rejection
        // when the generator rejects first.
        .catch((err) => { feedErrors.push(err); source.fail(err); }),
    );
  }

  const failAllSources = (err: unknown) => {
    for (const source of sources) source.fail(err);
  };

  // Start generation immediately so the upstream chunks are consumed as they arrive (never
  // all buffered). The generator reads file-by-file in insertion order.
  const generator = zip.generateInternalStream({
    type: "uint8array",
    compression: "STORE",
    streamFiles: true,
  });

  try {
    await new Promise<void>((resolve, reject) => {
      let writeFailed: unknown = null;

      generator.on("data", async (chunk: Uint8Array) => {
        generator.pause();
        try {
          await writable.write(chunk);
        } catch (err) {
          writeFailed = err;
        } finally {
          if (writeFailed) {
            reject(writeFailed);
          } else {
            generator.resume();
          }
        }
      });

      generator.on("end", () => {
        if (writeFailed) return; // already rejecting
        resolve();
      });

      generator.on("error", (err: Error) => {
        reject(err);
      });

      generator.resume();
    });
  } catch (err) {
    // The consumer has stopped draining. Release every producer that is parked on
    // backpressure before propagating, then let them settle so nothing is left running
    // against a dead writable.
    failAllSources(err);
    await Promise.allSettled(feeds);
    throw err;
  }

  await Promise.all(feeds);
  if (feedErrors.length > 0) throw feedErrors[0];
  onProgress?.("Finishing ZIP write…");
}

export async function streamZipToDisk(
  suggestedName: string,
  entries: StreamZipEntries,
  onProgress?: (msg: string) => void,
): Promise<boolean> {
  if (!canStreamSave()) {
    throw new Error("File System Access API (showSaveFilePicker) is not available in this browser.");
  }

  let handle: FileSystemFileHandleLike;
  try {
    // canStreamSave() above guarantees presence; TS cannot narrow through the helper.
    handle = await window.showSaveFilePicker!({
      suggestedName,
      types: [{ description: "ZIP archive", accept: { "application/zip": [".zip"] } }],
    });
  } catch (err) {
    if ((err as DOMException)?.name === "AbortError") return false; // user cancelled — not an error
    throw err;
  }

  // Acquire the real file handle before doing any asynchronous preparation. Chromium's
  // picker requires the original click gesture; a camera byte scan here would otherwise
  // turn a valid export into a SecurityError before the dialog appeared.
  const resolvedEntries = typeof entries === "function" ? await entries() : entries;
  const writable = await handle.createWritable();
  try {
    await runStreamToDisk(resolvedEntries, writable, onProgress);
    await writable.close();
    return true;
  } catch (err) {
    // abort(), never close(): on a FileSystemWritableFileStream close() is the COMMIT.
    // Closing here published however many bytes had been written under the operator's
    // chosen filename — a valid-looking archive holding a truncated recording. abort()
    // discards the partial file and releases the handle.
    await writable.abort().catch(() => {});
    throw err;
  }
}
