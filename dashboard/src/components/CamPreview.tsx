import { useEffect, useRef } from 'react';

interface Props {
  /** Either a real URL (MJPEG stream, JPEG still) or a `mock://` URL from MockTransport. */
  url: string;
  alt: string;
  className?: string;
}

/**
 * Camera preview / reference still.
 *
 * Against the real backend this is a plain <img> on the MJPEG endpoint
 * (`/api/cameras/{cam}/preview.mjpg`). Under `dev:mock` the transport hands
 * out `mock://` URLs and we draw canvas test patterns instead — the browser
 * never touches getUserMedia (cameras belong to the backend, SPEC §6.4).
 */
export function CamPreview({ url, alt, className }: Props) {
  if (url.startsWith('mock://')) return <MockCanvas url={url} alt={alt} className={className} />;
  return <img src={url} alt={alt} className={className} />;
}

function hashCode(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

const BARS = ['#c2c2c2', '#c2c200', '#00c2c2', '#00c200', '#c200c2', '#c20000', '#0000c2'];

function drawPattern(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  label: string,
  seed: number,
  tSec: number,
): void {
  const barW = w / BARS.length;
  for (let i = 0; i < BARS.length; i += 1) {
    ctx.fillStyle = BARS[i] ?? '#888';
    ctx.fillRect(i * barW, 0, barW + 1, h * 0.72);
  }
  ctx.fillStyle = '#101014';
  ctx.fillRect(0, h * 0.72, w, h * 0.28);
  // Moving box (animated previews) or fixed position seeded by the URL (stills).
  const phase = tSec > 0 ? (tSec * 0.25 + seed / 1000) % 1 : (seed % 1000) / 1000;
  const bw = Math.max(16, w * 0.12);
  const bx = phase * (w - bw);
  const by = (h * 0.5) + Math.sin(phase * Math.PI * 2) * (h * 0.18);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(bx, by, bw, bw * 0.75);
  ctx.fillStyle = '#ffffff';
  ctx.font = `${Math.max(10, Math.round(w / 26))}px sans-serif`;
  ctx.textBaseline = 'bottom';
  ctx.fillText(label, 6, h - 4);
}

function MockCanvas({ url, alt, className }: Props) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return;
    }
    const kind = parsed.hostname; // 'preview' | 'still'
    const id = parsed.pathname.replace(/^\//, '') || 'cam';
    const w = Math.max(80, Math.min(1280, Number(parsed.searchParams.get('w') ?? '480')));
    const h = Math.round((w * 9) / 16);
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    if (!ctx) return; // jsdom / headless without canvas: render nothing, no errors
    const seed = hashCode(url);
    const label = `${id} · MOCK ${w}×${h}`;
    if (kind === 'still') {
      drawPattern(ctx, w, h, label, seed, 0);
      return;
    }
    const fps = Math.max(1, Math.min(30, Number(parsed.searchParams.get('fps') ?? '10')));
    const started = Date.now();
    drawPattern(ctx, w, h, label, seed, 0.001);
    const timer = setInterval(() => {
      drawPattern(ctx, w, h, label, seed, (Date.now() - started) / 1000);
    }, Math.round(1000 / fps));
    return () => clearInterval(timer);
  }, [url]);

  return <canvas ref={ref} role="img" aria-label={alt} className={className} />;
}
