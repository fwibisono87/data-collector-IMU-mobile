// Robust WebM finalizer for MediaRecorder live streams.
//
// MediaRecorder writes WebM as a *live/streaming* bytestream: the Segment has an
// unset size, no final Duration in Info, a stale/absent SeekHead and no Cues. Players
// therefore can't determine the length, can't seek, and some report the tail as broken.
//
// The previous fix (vendored `fix-webm-duration`) re-encoded the ENTIRE EBML tree by
// hand, which could misalign cluster framing after the header — producing the
// "first second plays, the rest is unplayable" corruption. This module instead uses
// `ts-ebml`'s proven decoder + `tools.makeMetadataSeekable`, which preserves every
// cluster byte as-is and only re-writes the metadata (SeekHead/Info/Cues) into a fully
// seekable, playable file. It is the same recipe `ts-ebml` uses in its own browser
// regression suite, which asserts the output plays AND seeks.
//
// Safety: on ANY decode/encode failure this returns the original concatenated blob
// unchanged, so footage is never lost (mirrors the old no-op-on-failure guarantee).

// ts-ebml ships a browserified UMD bundle; `Decoder`/`Reader`/`tools` are the surface
// we need. Types come from types/ts-ebml-dist.d.ts.
import {
  Decoder,
  Reader,
  tools,
} from "ts-ebml/dist/EBML";

export interface FinalizeResult {
  blob: Blob;
  ok: boolean;
}

/**
 * Reassemble per-second MediaRecorder chunks into a single seekable WebM.
 * @param chunks ordered dataavailable slices (one continuous MediaRecorder stream)
 * @returns the final seekable WebM blob (or the raw concatenation on any failure)
 */
export async function finalizeWebm(chunks: Blob[]): Promise<FinalizeResult> {
  try {
    // Concatenate the full stream once; decoding the assembled buffer mirrors ts-ebml's
    // own test harness and lets the Reader compute the exact metadata/body split.
    const full = await new Blob(chunks).arrayBuffer();

    const reader = new Reader();
    const decoderInstance = new Decoder();
    const elms = decoderInstance.decode(full);
    // NB: pass EVERY element (not just masters) exactly like ts-ebml's own regression
    // harness — Reader inspects each element's type internally.
    for (const elm of elms) {
      reader.read(elm);
    }
    reader.stop();

    const refinedMetadataBuf = tools.makeMetadataSeekable(reader.metadatas, reader.duration, reader.cues);
    const body = full.slice(reader.metadataSize);

    return {
      blob: new Blob([refinedMetadataBuf, body], { type: "video/webm" }),
      ok: true,
    };
  } catch (e) {
    console.error("finalizeWebm failed — falling back to raw concatenation", e);
    return { blob: new Blob(chunks, { type: "video/webm" }), ok: false };
  }
}
