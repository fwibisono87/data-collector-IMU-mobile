import { CamPreview } from '../../components/CamPreview';
import { EMPTY_CAMERAS } from '../../lib/empty';
import { useSnapshot, useTransport } from '../../state/store';

/**
 * Camera strip while recording (SPEC §5.2): small live thumbnails from the
 * backend with a REC dot and fps; a dropped camera shows "lost —
 * reconnecting" (recording resumes into _partN automatically, backend side).
 */
export function CameraStrip() {
  const cameras = useSnapshot((s) => s.snapshot?.cameras ?? EMPTY_CAMERAS);
  const transport = useTransport();
  return (
    <section className="shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
      <h2 className="mb-1 text-xs font-black uppercase tracking-wider text-zinc-500">Cameras</h2>
      <div className="flex gap-2 overflow-x-auto" data-testid="camera-strip">
        {cameras.length === 0 && <p className="text-xs text-zinc-500">No cameras selected.</p>}
        {cameras.map((c) => {
          const live = c.state === 'live';
          return (
            <div
              key={c.cam_id}
              className={`relative w-40 shrink-0 overflow-hidden rounded border ${
                live ? 'border-zinc-700' : 'border-amber-500/60'
              }`}
              data-testid={`cam-strip-${c.cam_id}`}
            >
              <CamPreview
                url={transport.previewUrl(c.cam_id, 240, 5)}
                alt={`${c.cam_id} preview`}
                className="aspect-video w-full bg-black object-cover"
              />
              {c.recording && live && (
                <span
                  className="absolute left-1 top-1 inline-block h-2.5 w-2.5 animate-pulse rounded-full bg-red-500 shadow"
                  title="Recording"
                  aria-label="Recording"
                />
              )}
              <span className="absolute bottom-0 right-0 rounded-tl bg-black/70 px-1 text-[10px] font-bold tabular-nums text-zinc-200">
                {c.cam_id}
                {c.fps !== null ? ` ${c.fps.toFixed(0)}fps` : ''}
              </span>
              {!live && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/60 px-1 text-center text-[11px] font-black text-amber-300">
                  {c.state === 'lost' ? 'lost — reconnecting' : c.state.toUpperCase()}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
