import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { errorMessage } from '../../api/errors';
import type { Camera, CameraDevice, Reference } from '../../api/types';
import { CamPreview } from '../../components/CamPreview';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { VerdictBadge } from '../../components/VerdictBadge';
import { EMPTY_CAMERAS } from '../../lib/empty';
import { useTransport, useSnapshot } from '../../state/store';

const MAX_CAMERAS = 5; // PUT /api/cameras/selection refuses more (API.md)

/**
 * Cameras panel (SPEC §5.1): device checklist, live preview tiles from the
 * BACKEND (never getUserMedia — the webcam is owned by the camera service),
 * reference stills and the link into calibration.
 */
export function CamerasPanel() {
  const transport = useTransport();
  const cameras = useSnapshot((s) => s.snapshot?.cameras ?? EMPTY_CAMERAS);
  const state = useSnapshot((s) => s.snapshot?.state ?? null);
  const recording = state === 'RECORDING' || state === 'FINALIZING' || state === 'VALIDATING';

  const [devices, setDevices] = useState<CameraDevice[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refDialog, setRefDialog] = useState<Reference | null>(null);

  useEffect(() => {
    let alive = true;
    transport
      .listCameraDevices()
      .then((d) => {
        if (alive) setDevices(d.devices);
      })
      .catch((e) => {
        if (alive) setError(errorMessage(e));
      });
    return () => {
      alive = false;
    };
  }, [transport]);

  // Selected keys (from the snapshot) merged over the device list so a camera
  // stays visible even if the last re-enumeration missed its device.
  const selectedKeys = cameras.map((c) => c.device_key);
  const rows: Array<{ key: string; label: string; backend: string; cam: Camera | null }> = [
    ...devices.map((d) => ({
      key: d.device_key,
      label: d.label,
      backend: d.backend,
      cam: cameras.find((c) => c.device_key === d.device_key) ?? null,
    })),
    ...cameras
      .filter((c) => !devices.some((d) => d.device_key === c.device_key))
      .map((c) => ({ key: c.device_key, label: c.label, backend: '?', cam: c })),
  ];

  const toggle = (deviceKey: string) => {
    if (recording) return;
    const isSelected = selectedKeys.includes(deviceKey);
    let next: string[];
    if (isSelected) {
      next = selectedKeys.filter((k) => k !== deviceKey);
    } else {
      if (selectedKeys.length >= MAX_CAMERAS) {
        setError(`At most ${MAX_CAMERAS} cameras can be selected.`);
        return;
      }
      next = [...selectedKeys, deviceKey];
    }
    setBusy(true);
    transport
      .putCameraSelection(next.map((k) => ({ device_key: k })))
      .then(() => setError(null))
      .catch((e) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const takeStills = () => {
    setBusy(true);
    transport
      .createReference({})
      .then((ref) => {
        setError(null);
        setRefDialog(ref);
      })
      .catch((e) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="text-sm font-black uppercase tracking-wider text-zinc-400">
          Cameras{' '}
          <span className="text-zinc-500">
            ({cameras.filter((c) => c.state === 'live').length}/{cameras.length} live)
          </span>
        </h2>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={busy || cameras.length === 0}
            className="rounded border border-zinc-600 px-2 py-1 text-xs font-bold hover:bg-zinc-800 disabled:opacity-40"
            onClick={takeStills}
          >
            Take reference stills
          </button>
          <Link
            to="/calibration"
            className="rounded border border-cyan-500/60 px-2 py-1 text-xs font-bold text-cyan-300 hover:bg-cyan-500/10"
          >
            Calibrate
          </Link>
        </div>
      </div>

      {error && (
        <p className="mb-2 text-xs font-semibold text-red-400" role="alert">
          {error}
        </p>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-2 gap-2 overflow-y-auto pr-1">
        {/* Device checklist */}
        <div className="flex flex-col gap-1" data-testid="camera-checklist">
          {rows.length === 0 && (
            <p className="text-sm text-zinc-500">No camera devices found by the backend.</p>
          )}
          {rows.map((r) => {
            const checked = r.cam !== null;
            const full = !checked && selectedKeys.length >= MAX_CAMERAS;
            return (
              <label
                key={r.key}
                className={`flex items-center gap-2 rounded border px-2 py-1.5 text-sm ${
                  checked ? 'border-cyan-500/50 bg-cyan-500/5' : 'border-zinc-800 bg-zinc-950/40'
                } ${recording || full ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:border-zinc-600'}`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={recording || full}
                  onChange={() => toggle(r.key)}
                  aria-label={`Select ${r.label}`}
                />
                <span className="min-w-0 flex-1 truncate font-semibold">{r.label}</span>
                {r.cam && (
                  <span className="text-xs font-black tabular-nums text-cyan-300">{r.cam.cam_id}</span>
                )}
                <span className="text-[10px] uppercase text-zinc-600">{r.backend}</span>
              </label>
            );
          })}
          {recording && (
            <p className="text-xs text-zinc-500">Camera selection is locked while recording.</p>
          )}
        </div>

        {/* Preview tiles for the selected cameras */}
        <div className="grid auto-rows-min grid-cols-1 gap-2">
          {cameras.map((c) => (
            <CameraTile key={c.cam_id} cam={c} />
          ))}
        </div>
      </div>

      <ReferenceDialog
        reference={refDialog}
        onClose={() => setRefDialog(null)}
        onMakeActive={(refId) =>
          transport
            .activateReference(refId)
            .then(() => setRefDialog(null))
            .catch((e) => setError(errorMessage(e)))
        }
      />
    </section>
  );
}

function CameraTile({ cam }: { cam: Camera }) {
  const transport = useTransport();
  const live = cam.state === 'live';
  return (
    <div
      className={`overflow-hidden rounded border ${live ? 'border-zinc-700' : 'border-amber-500/50'}`}
      data-testid={`camera-tile-${cam.cam_id}`}
    >
      <div className="relative bg-black">
        <CamPreview
          url={transport.previewUrl(cam.cam_id, 480, 10)}
          alt={`${cam.cam_id} live preview`}
          className="aspect-video w-full object-contain"
        />
        {!live && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/60 text-sm font-black text-amber-300">
            {cam.state === 'lost' ? 'lost — reconnecting' : cam.state.toUpperCase()}
          </div>
        )}
      </div>
      <div className="flex items-center justify-between gap-2 bg-zinc-950 px-2 py-1 text-xs">
        <span className="font-black text-zinc-100">{cam.cam_id}</span>
        <span className="tabular-nums text-zinc-400">
          {cam.width && cam.height ? `${cam.width}×${cam.height}` : '—'}
          {cam.fps !== null ? ` @ ${cam.fps.toFixed(0)} fps` : ''}
        </span>
        <VerdictBadge deviceKey={cam.device_key} camId={cam.cam_id} fallback={cam.drift} />
      </div>
      {cam.error && <p className="bg-zinc-950 px-2 pb-1 text-[11px] text-red-400">{cam.error}</p>}
    </div>
  );
}

function ReferenceDialog({
  reference,
  onClose,
  onMakeActive,
}: {
  reference: Reference | null;
  onClose: () => void;
  onMakeActive: (refId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  if (!reference) return null;
  return (
    <ConfirmDialog
      open
      title={`Reference stills captured — ${reference.name}`}
      body={
        <div>
          <div className="grid grid-cols-3 gap-2">
            {reference.cameras.map((c) => (
              <figure key={c.device_key} className="overflow-hidden rounded border border-zinc-700">
                {reference.thumb_urls?.[c.device_key] ? (
                  <CamPreview
                    url={reference.thumb_urls[c.device_key]}
                    alt={`${c.label} reference still`}
                    className="aspect-video w-full bg-black object-contain"
                  />
                ) : (
                  <div className="flex aspect-video w-full items-center justify-center bg-black text-xs text-zinc-500">
                    no thumbnail
                  </div>
                )}
                <figcaption className="bg-zinc-950 px-1 py-0.5 text-[10px] text-zinc-400">
                  {c.cam_id_at_capture} · skew {c.skew_ms} ms
                </figcaption>
              </figure>
            ))}
          </div>
          <p className="mt-2 text-xs text-zinc-400">
            The set is saved but NOT active yet — the readiness check keeps using the previous
            reference until you activate this one.
          </p>
        </div>
      }
      confirmLabel="Make active"
      busy={busy}
      onConfirm={() => {
        setBusy(true);
        onMakeActive(reference.ref_id);
      }}
      onCancel={onClose}
    />
  );
}
