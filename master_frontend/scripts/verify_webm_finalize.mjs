#!/usr/bin/env node
// Regression check for src/lib/webm_seekable.ts — the guard this codebase kept missing.
//
// "Video recorded but only the first second plays" has shipped twice. Both times the
// saved file was structurally wrong in a way nothing asserted against, so it looked fine
// until an operator tried to open it. These assertions are what a green build should have
// been telling us:
//
//   1. a finalized file gains a real Duration and Cues, and its frames are untouched
//   2. input that is NOT a WebM stream is passed through byte-for-byte, never truncated
//   3. two EBML headers in one chunk range are detected, even across chunk seams
//
// (2) is not hypothetical: ts-ebml does not reject arbitrary bytes — it parses them into
// "unknown" elements and reports a nonsense metadataSize. An earlier draft of the
// finalizer trusted that and wrote 37,973 bytes for a 50,000-byte input.
//
// Run:
//   node scripts/verify_webm_finalize.mjs [path/to/a/session.webm]
//
// The optional argument is any unfinalized MediaRecorder .webm; test (1) is skipped
// without one, since no such file is checked into the repo. Tests (2) and (3) always run.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import crypto from "node:crypto";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// Must live inside the project: the compiled module does `require("ts-ebml/dist/EBML")`,
// which only resolves from a directory under this package's node_modules chain.
const outDir = mkdtempSync(join(root, "node_modules", ".webm-verify-"));
let failures = 0;

function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
}

const collect = () => {
  const parts = [];
  return {
    sink: async (c) => {
      parts.push(Buffer.from(c instanceof Blob ? await c.arrayBuffer() : c));
    },
    get bytes() { return Buffer.concat(parts); },
  };
};

const sliceReader = (buf, size) => async (onChunk) => {
  for (let o = 0; o < buf.length; o += size) {
    await onChunk(new Blob([buf.subarray(o, Math.min(o + size, buf.length))]));
  }
};

const countMagic = (buf, magic) => {
  let n = 0, i = buf.indexOf(magic);
  while (i !== -1) { n++; i = buf.indexOf(magic, i + 1); }
  return n;
};

const EBML_HDR = Buffer.from([0x1a, 0x45, 0xdf, 0xa3]);
const CUES = Buffer.from([0x1c, 0x53, 0xbb, 0x6b]);

try {
  // Compile the module under test into the project so `ts-ebml` resolves.
  execFileSync("npx", [
    "tsc", "src/lib/webm_seekable.ts", "--outDir", outDir, "--module", "commonjs",
    "--target", "es2020", "--moduleResolution", "node", "--esModuleInterop", "--skipLibCheck",
  ], { cwd: root, stdio: "pipe" });

  const { finalizeWebmStream } = await import(join(outDir, "webm_seekable.js"));

  // (2) Non-WebM input must survive untouched rather than be silently truncated.
  {
    const junk = crypto.randomBytes(50_000);
    const out = collect();
    const res = await finalizeWebmStream(sliceReader(junk, 4096), out.sink);
    check("non-WebM input falls back without losing bytes",
      res.ok === false && out.bytes.equals(junk),
      `ok=${res.ok} in=${junk.length} out=${out.bytes.length}`);
  }

  // (3) A second recorder's header must be seen even when it straddles chunk seams.
  {
    const two = Buffer.concat([
      EBML_HDR, crypto.randomBytes(100), EBML_HDR, crypto.randomBytes(100),
    ]);
    const out = collect();
    const res = await finalizeWebmStream(sliceReader(two, 3), out.sink);
    check("two EBML headers detected across 3-byte chunk seams",
      res.ebmlHeaders === 2, `got ${res.ebmlHeaders}`);
    check("a two-recorder range is passed through, not mislabelled as finalized",
      res.ok === false && out.bytes.equals(two));
  }

  // (1) A genuine unfinalized recording gains Duration + Cues, frames untouched.
  const fixture = process.argv[2];
  if (!fixture) {
    console.log("SKIP  finalize a real recording (pass a .webm path to enable)");
  } else {
    const src = readFileSync(fixture);
    const out = collect();
    const res = await finalizeWebmStream(sliceReader(src, 4096), out.sink);
    const buf = out.bytes;
    writeFileSync(join(outDir, "finalized.webm"), buf);
    check("real recording finalizes", res.ok === true, JSON.stringify(res));
    check("output declares a duration", (res.durationMs ?? 0) > 0, `${res.durationMs}ms`);
    check("output has Cues (seekable)", countMagic(buf, CUES) > 0);
    check("output has exactly one EBML header", countMagic(buf, EBML_HDR) === 1);
    check("frames are byte-identical (only metadata was rewritten)",
      src.subarray(src.length - 8192).equals(buf.subarray(buf.length - 8192)));
  }
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
