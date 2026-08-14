// Robust WebM finalizer for MediaRecorder live streams.
//
// MediaRecorder writes WebM as a *live/streaming* bytestream: the Segment has an
// unset size, no final Duration in Info, a stale/absent SeekHead and no Cues. Players
// therefore can't determine the length, can't seek, and some report the tail as broken.
// `ffprobe` on an unfinalized session file reports `duration=N/A`.
//
// This module uses `ts-ebml`'s proven decoder + `tools.makeMetadataSeekable`, which
// preserves every cluster byte as-is and only re-writes the metadata (SeekHead/Info/Cues)
// into a fully seekable, playable file. It is the same recipe `ts-ebml` uses in its own
// browser regression suite, which asserts the output plays AND seeks.
//
// WHY STREAMING (do not "simplify" this back into a Blob[] API):
// the previous signature took the whole recording as `Blob[]` and did
// `new Blob(chunks).arrayBuffer()`. At 130-200MB per camera across four cameras that is
// the full-heap reassembly which exhausted the renderer and produced "Application Error"
// — the exact failure the streamChunks rewrite exists to prevent. Instead we make two
// passes over IndexedDB and never hold more than one chunk plus one cluster of parsed
// elements: ts-ebml's decoder trims its consumed buffer, and its reader drains retained
// elements at every cluster boundary.
//
// Safety: on ANY decode/encode failure this falls back to streaming the raw bytes
// through unchanged, so footage is never lost. The fallback can only be taken before
// the first byte is written, so it can never emit a half-finalized file.

import {
  Decoder,
  Reader,
  tools,
} from "ts-ebml/dist/EBML";

/** Feeds every chunk of one recording, in order, to `onChunk`. Callable twice. */
export type ChunkReader = (onChunk: (blob: Blob) => Promise<void>) => Promise<void>;
export type Sink = (chunk: Uint8Array | Blob) => Promise<void>;

export interface FinalizeResult {
  /** true when the metadata was rebuilt; false when raw bytes were passed through. */
  ok: boolean;
  bytesWritten: number;
  durationMs: number | null;
  /**
   * How many EBML headers were seen. Exactly 1 is healthy. More than 1 means two
   * MediaRecorders wrote into this chunk range — the corruption fixed in Aug 2026 —
   * and the file will stop playing at the first header boundary.
   */
  ebmlHeaders: number;
  /** Set when the finalize failed and the raw bytes were streamed instead. */
  error?: string;
}

const EBML_MAGIC = [0x1a, 0x45, 0xdf, 0xa3];

/** Counts EBML headers across chunk boundaries by carrying the last 3 bytes over. */
class HeaderCounter {
  count = 0;
  private carry: number[] = [];

  feed(bytes: Uint8Array): void {
    // Only the seam needs the carry; the body is scanned in place.
    const seam = [...this.carry, ...Array.from(bytes.subarray(0, 3))];
    for (let i = 0; i + 4 <= seam.length && i < this.carry.length; i++) {
      if (EBML_MAGIC.every((b, k) => seam[i + k] === b)) this.count++;
    }
    for (let i = 0; i + 4 <= bytes.length; i++) {
      if (
        bytes[i] === 0x1a && bytes[i + 1] === 0x45 &&
        bytes[i + 2] === 0xdf && bytes[i + 3] === 0xa3
      ) this.count++;
    }
    this.carry = Array.from(bytes.subarray(Math.max(0, bytes.length - 3)));
  }
}

async function passThrough(read: ChunkReader, sink: Sink): Promise<number> {
  let bytes = 0;
  await read(async (blob) => {
    bytes += blob.size;
    await sink(blob);
  });
  return bytes;
}

/**
 * Rewrite one recording's metadata and stream it to `sink`, holding only one chunk in
 * memory at a time.
 *
 * @param read  yields the recording's chunks in order; MUST be replayable, because the
 *              metadata can only be computed after every cluster has been seen.
 * @param sink  receives the finalized bytes, header first.
 */
export async function finalizeWebmStream(read: ChunkReader, sink: Sink): Promise<FinalizeResult> {
  const counter = new HeaderCounter();
  let refined: ArrayBuffer;
  let metadataSize: number;
  let durationMs: number | null = null;

  // Pass 1 — parse only. Nothing is written, so any failure here can still fall back.
  try {
    const reader = new Reader();
    const decoder = new Decoder();
    let totalBytes = 0;
    await read(async (blob) => {
      const buf = await blob.arrayBuffer();
      totalBytes += buf.byteLength;
      counter.feed(new Uint8Array(buf));
      for (const elm of decoder.decode(buf)) {
        reader.read(elm);
      }
    });
    reader.stop();

    // ts-ebml does NOT reliably throw on input that is not a WebM live stream: it happily
    // parses arbitrary bytes into "unknown" elements and yields a nonsense metadataSize.
    // Trusting that silently truncated the output (50,000 bytes in, 37,973 out) — data
    // loss dressed up as success. Everything below must hold before a byte is written.
    if (!reader.metadatas.length) {
      throw new Error("no metadata elements parsed");
    }
    // A real WebM declares its tracks in the metadata. Bytes that are not WebM decode
    // into elements named "unknown", so this is what separates a genuine recording from
    // something ts-ebml merely failed to reject.
    if (!reader.metadatas.some(e => e.name === "TrackEntry")) {
      throw new Error("no track metadata found — not a WebM stream");
    }
    if (!(reader.metadataSize > 0) || reader.metadataSize >= totalBytes) {
      throw new Error(`implausible metadataSize ${reader.metadataSize} of ${totalBytes}`);
    }
    // More than one header means two MediaRecorders wrote into this range. The metadata
    // would describe only the first, so finalizing would mislabel the file. Pass the
    // bytes through untouched and let tools/recover_video.py separate them.
    if (counter.count !== 1) {
      throw new Error(`${counter.count} EBML headers — not a single recording`);
    }

    refined = tools.makeMetadataSeekable(reader.metadatas, reader.duration, reader.cues);
    if (!refined || refined.byteLength === 0) {
      throw new Error("makeMetadataSeekable produced an empty header");
    }
    metadataSize = reader.metadataSize;
    // reader.duration is in timestampScale units (nanoseconds by default).
    const ns = reader.duration * reader.timestampScale;
    durationMs = Number.isFinite(ns) && ns > 0 ? ns / 1e6 : null;
  } catch (e) {
    console.error("finalizeWebmStream: metadata rebuild failed, streaming raw", e);
    const bytesWritten = await passThrough(read, sink);
    return {
      ok: false,
      bytesWritten,
      durationMs: null,
      ebmlHeaders: counter.count,
      error: String(e),
    };
  }

  // Pass 2 — write the rebuilt header, then the original clusters byte-for-byte.
  let bytesWritten = 0;
  await sink(new Uint8Array(refined));
  bytesWritten += refined.byteLength;

  let skip = metadataSize;
  await read(async (blob) => {
    if (skip >= blob.size) {
      skip -= blob.size;
      return;
    }
    const piece = skip > 0 ? blob.slice(skip) : blob;
    skip = 0;
    bytesWritten += piece.size;
    await sink(piece);
  });

  return { ok: true, bytesWritten, durationMs, ebmlHeaders: counter.count };
}
