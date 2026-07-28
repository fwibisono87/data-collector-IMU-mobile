// Synthesised alert tones — no npm dependency, no asset, works offline.
// Browsers require a user gesture before audio can start; armAudio() is called from the
// Connect button's onClick, which always precedes any alert.
export type AlertKind = "device_lost" | "device_back" | "ws_lost" | "ws_back" | "test";

let ctx: AudioContext | null = null;
const MUTE_KEY = "alertsMuted";

export function armAudio(): void {
  if (typeof window === "undefined") return;
  ctx ??= new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
  if (ctx.state === "suspended") void ctx.resume();
}

export function isMuted(): boolean {
  return typeof window !== "undefined" && localStorage.getItem(MUTE_KEY) === "1";
}

export function setMuted(m: boolean): void {
  localStorage.setItem(MUTE_KEY, m ? "1" : "0");
}

function beep(freq: number, durMs: number, atMs: number, gain = 0.14, type: OscillatorType = "square") {
  if (!ctx) return;
  const t0 = ctx.currentTime + atMs / 1000;
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.type = type;
  osc.frequency.value = freq;
  g.gain.setValueAtTime(0, t0);
  g.gain.linearRampToValueAtTime(gain, t0 + 0.01);
  g.gain.setValueAtTime(gain, t0 + durMs / 1000 - 0.02);
  g.gain.linearRampToValueAtTime(0, t0 + durMs / 1000);
  osc.connect(g).connect(ctx.destination);
  osc.start(t0);
  osc.stop(t0 + durMs / 1000 + 0.02);
}

const PATTERNS: Record<AlertKind, Array<[number, number, number]>> = {
  device_lost: [[880, 130, 0], [660, 130, 170], [440, 220, 340]],
  device_back: [[660, 90, 0], [990, 140, 110]],
  ws_lost:     [[240, 200, 0], [240, 200, 260], [240, 320, 520]],
  ws_back:     [[880, 120, 0]],
  test:        [[740, 150, 0]],
};

export function playAlert(kind: AlertKind, force = false): void {
  if (!force && isMuted()) return;
  armAudio();
  PATTERNS[kind].forEach(([f, d, at]) => beep(f, d, at));
}

// Repeats `kind` every `everyMs` until the returned stop() is called.
export function startRepeating(kind: AlertKind, everyMs = 4000): () => void {
  playAlert(kind);
  const id = setInterval(() => playAlert(kind), everyMs);
  return () => clearInterval(id);
}
