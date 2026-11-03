import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage } from '../api/errors';
import type { Transport } from '../api/transport';
import type { Camera, Reference, ReferenceCamera } from '../api/types';
import { CamPreview } from '../components/CamPreview';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PanelBoundary } from '../components/PanelBoundary';
import { VerdictBadge } from '../components/VerdictBadge';
import { EMPTY_CAMERAS } from '../lib/empty';
import { fmtClock } from '../lib/format';
import { loadNumberPref, loadPref, savePref } from '../state/prefs';
import { useSnapshot, useTransport } from '../state/store';

export type OverlayMode = 'blend' | 'difference' | 'blink' | 'side';

const MODES: Array<{ id: OverlayMode; label: string }> = [
  { id: 'blend', label: 'Blend' },
  { id: 'difference', label: 'Difference' },
  { id: 'blink', label: 'Blink' },
  { id: 'side', label: 'Side by side' },
];

interface Override {
  opacity: number;
  mode: OverlayMode;
  edges: boolean;
}

function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

/**
 * Calibration mode (CALIBRATION.md, UI section): reference picker + a grid of
 * camera panels with the reference still as a ghost overlay over the live
 * preview. Verdict badges are fed by the isolated drift store (2 Hz
 * `calibration.drift` after `calibration.watch`). Read-only during recording:
 * taking stills stays allowed, selection changes do not exist on this page.
 */
export function CalibrationView() {
  const transport = useTransport();
  const navigate = useNavigate();
  const cameras = useSnapshot((s) => s.snapshot?.cameras ?? EMPTY_CAMERAS);
  const state = useSnapshot((s) => s.snapshot?.state ?? null);
  const recording = state === 'RECORDING';

  const [refs, setRefs] = useState<Reference[]>([]);
  const [activeRefId, setActiveRefId] = useState<string | null>(null);
  const [selectedRefId, setSelectedRefId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<Reference | null>(null);
  const [newRefDialog, setNewRefDialog] = useState<Reference | null>(null);
  const [fullscreen, setFullscreen] = useState<string | null>(null);

  // Global overlay settings — pure view preferences, persisted in localStorage.
  const [opacity, setOpacity] = useState(() => loadNumberPref('overlayOpacity', 50));
  const [mode, setMode] = useState<OverlayMode>(() => {
    const raw = loadPref('overlayMode', 'blend');
    return MODES.some((m) => m.id === raw) ? (raw as OverlayMode) : 'blend';
  });
  const [edges, setEdges] = useState(() => loadPref('overlayEdges', '0') === '1');
  const [grid, setGrid] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, Override>>({});

  // Blink phase: swaps reference/live every 500 ms; Space toggles manually.
  const [blinkOn, setBlinkOn] = useState(true);

  const loadRefs = useCallback(() => {
    transport
      .listReferences()
      .then((res) => {
        setRefs(res.references);
        setActiveRefId(res.active_ref_id);
        setSelectedRefId((cur) => {
          if (cur && res.references.some((r) => r.ref_id === cur)) return cur;
          return res.active_ref_id ?? res.references[0]?.ref_id ?? null;
        });
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, [transport]);

  useEffect(loadRefs, [loadRefs]);

  const selectedRef = useMemo(
    () => refs.find((r) => r.ref_id === selectedRefId) ?? null,
    [refs, selectedRefId],
  );

  // watch on mount / ref change, unwatch on unmount (API.md).
  useEffect(() => {
    transport.watchCalibration(selectedRefId ?? 'active').catch(() => {
      // offline: badges fall back to the snapshot's drift field
    });
    return () => {
      transport.unwatchCalibration().catch(() => {});
    };
  }, [transport, selectedRefId]);

  const anyBlink =
    (selectedRef?.cameras ?? []).filter((c) => effectiveMode(overrides[c.device_key], mode) === 'blink')
      .length > 0;

  useEffect(() => {
    if (!anyBlink) return;
    const t = setInterval(() => setBlinkOn((v) => !v), 500);
    return () => clearInterval(t);
  }, [anyBlink]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== 'Space' && e.key !== ' ') return;
      if (isTypingTarget(e.target)) return;
      if (!anyBlink) return;
      e.preventDefault();
      setBlinkOn((v) => !v);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [anyBlink]);

  const run = (p: Promise<unknown>) => {
    setBusy(true);
    p.catch((e) => setError(errorMessage(e))).finally(() => {
      setBusy(false);
      loadRefs();
    });
  };

  const takeReference = () => {
    setBusy(true);
    transport
      .createReference({})
      .then((ref) => {
        setNewRefDialog(ref);
        loadRefs();
      })
      .catch((e) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const gridCols =
    (selectedRef?.cameras.length ?? 0) <= 1
      ? 'grid-cols-1'
      : (selectedRef?.cameras.length ?? 0) <= 4
        ? 'grid-cols-2'
        : 'grid-cols-3';

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2" data-testid="calibration-view">
      {recording && (
        <p className="shrink-0 rounded border border-red-500/40 bg-red-950/40 px-3 py-1 text-sm font-bold text-red-200">
          Recording — camera selection is locked. Taking reference stills is allowed and does not
          disturb the recording.
        </p>
      )}

      <PanelBoundary name="Reference picker" className="shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
        <div className="flex items-center gap-2">
          <h2 className="shrink-0 text-sm font-black uppercase tracking-wider text-zinc-400">References</h2>
          <div className="flex min-w-0 flex-1 gap-2 overflow-x-auto" data-testid="ref-picker">
            {refs.length === 0 && <p className="text-sm text-zinc-500">No reference sets yet — take one.</p>}
            {refs.map((r) => (
              <RefCard
                key={r.ref_id}
                reference={r}
                selected={r.ref_id === selectedRefId}
                active={r.ref_id === activeRefId}
                renaming={renaming === r.ref_id}
                renameValue={renameValue}
                setRenameValue={setRenameValue}
                onSelect={() => setSelectedRefId(r.ref_id)}
                onStartRename={() => {
                  setRenaming(r.ref_id);
                  setRenameValue(r.name);
                }}
                onCancelRename={() => setRenaming(null)}
                onCommitRename={() => {
                  setRenaming(null);
                  run(transport.renameReference(r.ref_id, renameValue));
                }}
                onActivate={() => run(transport.activateReference(r.ref_id))}
                onDelete={() => {
                  if (r.ref_id === activeRefId) {
                    setError('The active reference cannot be deleted — deactivate it first.');
                    return;
                  }
                  setConfirmDelete(r);
                }}
              />
            ))}
          </div>
          <div className="flex shrink-0 flex-col gap-1">
            <button
              type="button"
              disabled={busy || cameras.length === 0}
              className="rounded border border-zinc-600 px-2 py-1 text-xs font-bold hover:bg-zinc-800 disabled:opacity-40"
              onClick={takeReference}
            >
              Take new reference
            </button>
            <button
              type="button"
              disabled={busy || !selectedRefId || selectedRefId === activeRefId}
              className="rounded border border-green-600/60 px-2 py-1 text-xs font-bold text-green-300 hover:bg-green-500/10 disabled:opacity-40"
              onClick={() => selectedRefId && run(transport.activateReference(selectedRefId))}
            >
              Make active
            </button>
            <button
              type="button"
              className="rounded border border-zinc-700 px-2 py-1 text-xs font-bold text-zinc-300 hover:bg-zinc-800"
              onClick={() => navigate('/')}
            >
              Done
            </button>
          </div>
        </div>
        {error && (
          <p className="mt-1 text-xs font-semibold text-red-400" role="alert">
            {error}
          </p>
        )}
      </PanelBoundary>

      <PanelBoundary name="Overlay controls" className="shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <label className="flex items-center gap-2">
            <span className="text-xs font-bold uppercase text-zinc-500">Opacity</span>
            <input
              type="range"
              min={0}
              max={100}
              value={opacity}
              aria-label="Overlay opacity"
              data-testid="opacity-slider"
              className="w-40"
              onChange={(e) => {
                const v = Number(e.target.value);
                setOpacity(v);
                savePref('overlayOpacity', String(v));
              }}
            />
            <span className="w-10 tabular-nums text-zinc-300">{opacity}%</span>
          </label>
          <div className="flex items-center gap-1" role="group" aria-label="Overlay mode">
            <span className="mr-1 text-xs font-bold uppercase text-zinc-500">Mode</span>
            {MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                aria-pressed={mode === m.id}
                data-testid={`mode-${m.id}`}
                className={`rounded px-2 py-1 text-xs font-bold ${
                  mode === m.id ? 'bg-cyan-500 text-black' : 'border border-zinc-700 text-zinc-300 hover:bg-zinc-800'
                }`}
                onClick={() => {
                  setMode(m.id);
                  savePref('overlayMode', m.id);
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={edges}
              data-testid="edges-toggle"
              onChange={(e) => {
                setEdges(e.target.checked);
                savePref('overlayEdges', e.target.checked ? '1' : '0');
              }}
            />
            <span className="font-semibold text-zinc-300">Edges only</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={grid} data-testid="grid-toggle" onChange={(e) => setGrid(e.target.checked)} />
            <span className="font-semibold text-zinc-300">Grid / crosshair</span>
          </label>
          {anyBlink && <span className="text-xs text-zinc-500">Space toggles blink manually</span>}
        </div>
      </PanelBoundary>

      <PanelBoundary name="Camera panels" className="min-h-0 flex-1">
        <div className={`grid h-full min-h-0 auto-rows-fr gap-2 overflow-y-auto ${gridCols}`} data-testid="cam-panels">
          {(selectedRef?.cameras ?? []).map((rc) => (
            <CameraPanel
              key={rc.device_key}
              refCam={rc}
              reference={selectedRef as Reference}
              live={cameras.find((c) => c.device_key === rc.device_key) ?? null}
              fullscreen={fullscreen === rc.device_key}
              onToggleFullscreen={() =>
                setFullscreen((f) => (f === rc.device_key ? null : rc.device_key))
              }
              globalSettings={{ opacity, mode, edges }}
              override={overrides[rc.device_key] ?? null}
              onOverrideChange={(o) =>
                setOverrides((prev) => {
                  const next = { ...prev };
                  if (o === null) delete next[rc.device_key];
                  else next[rc.device_key] = o;
                  return next;
                })
              }
              grid={grid}
              blinkOn={blinkOn}
              transport={transport}
            />
          ))}
          {!selectedRef && (
            <p className="col-span-full text-sm text-zinc-500">
              Select a reference set above to compare against the live cameras.
            </p>
          )}
        </div>
      </PanelBoundary>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={`Delete reference "${confirmDelete?.name ?? ''}"?`}
        body="The stills are removed from disk. This cannot be undone."
        confirmLabel="Delete"
        danger
        busy={busy}
        onConfirm={() => {
          const r = confirmDelete;
          setConfirmDelete(null);
          if (r) run(transport.deleteReference(r.ref_id));
        }}
        onCancel={() => setConfirmDelete(null)}
      />

      <ConfirmDialog
        open={newRefDialog !== null}
        title={`New reference captured — ${newRefDialog?.name ?? ''}`}
        body={
          <div className="grid grid-cols-3 gap-2">
            {(newRefDialog?.cameras ?? []).map((c) => (
              <figure key={c.device_key} className="overflow-hidden rounded border border-zinc-700">
                {newRefDialog?.thumb_urls?.[c.device_key] ? (
                  <CamPreview
                    url={newRefDialog.thumb_urls[c.device_key]}
                    alt={`${c.label} still`}
                    className="aspect-video w-full bg-black object-contain"
                  />
                ) : (
                  <div className="aspect-video w-full bg-black" />
                )}
                <figcaption className="bg-zinc-950 px-1 py-0.5 text-[10px] text-zinc-400">
                  {c.cam_id_at_capture} · skew {c.skew_ms} ms
                </figcaption>
              </figure>
            ))}
          </div>
        }
        confirmLabel="Make active"
        onConfirm={() => {
          const r = newRefDialog;
          setNewRefDialog(null);
          if (r) run(transport.activateReference(r.ref_id));
        }}
        onCancel={() => setNewRefDialog(null)}
      />
    </div>
  );
}

function effectiveMode(o: Override | null, globalMode: OverlayMode): OverlayMode {
  return o ? o.mode : globalMode;
}

function RefCard({
  reference,
  selected,
  active,
  renaming,
  renameValue,
  setRenameValue,
  onSelect,
  onStartRename,
  onCancelRename,
  onCommitRename,
  onActivate,
  onDelete,
}: {
  reference: Reference;
  selected: boolean;
  active: boolean;
  renaming: boolean;
  renameValue: string;
  setRenameValue: (v: string) => void;
  onSelect: () => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onCommitRename: () => void;
  onActivate: () => void;
  onDelete: () => void;
}) {
  const thumb = reference.cameras[0] ? reference.thumb_urls?.[reference.cameras[0].device_key] : null;
  return (
    <div
      className={`flex w-52 shrink-0 items-center gap-2 rounded border p-1.5 ${
        selected ? 'border-cyan-400 bg-cyan-500/10' : 'border-zinc-700 bg-zinc-950 hover:border-zinc-500'
      }`}
      data-testid={`ref-card-${reference.ref_id}`}
    >
      <button type="button" className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={onSelect} aria-label={`Show reference ${reference.name}`}>
        {thumb ? (
          <CamPreview url={thumb} alt="" className="h-10 w-16 shrink-0 rounded bg-black object-cover" />
        ) : (
          <span className="h-10 w-16 shrink-0 rounded bg-black" />
        )}
        <span className="min-w-0">
          {renaming ? (
            <input
              type="text"
              value={renameValue}
              autoFocus
              className="w-full rounded border border-zinc-600 bg-zinc-950 px-1 text-xs"
              aria-label="Reference name"
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === 'Enter') onCommitRename();
                if (e.key === 'Escape') onCancelRename();
              }}
              onBlur={onCommitRename}
            />
          ) : (
            <span className="block truncate text-xs font-bold text-zinc-100">{reference.name}</span>
          )}
          <span className="block text-[10px] tabular-nums text-zinc-500">
            {fmtClock(reference.created_at_ms)}
          </span>
          {active && (
            <span className="mt-0.5 inline-block rounded bg-green-500/20 px-1 text-[10px] font-black text-green-300">
              ACTIVE
            </span>
          )}
        </span>
      </button>
      <span className="flex shrink-0 flex-col gap-0.5">
        <button type="button" aria-label={`Rename ${reference.name}`} className="rounded px-1 text-xs hover:bg-zinc-800" onClick={renaming ? onCommitRename : onStartRename}>
          ✎
        </button>
        <button type="button" aria-label={`Activate ${reference.name}`} disabled={active} className="rounded px-1 text-xs hover:bg-zinc-800 disabled:opacity-30" onClick={onActivate}>
          ★
        </button>
        <button type="button" aria-label={`Delete ${reference.name}`} className="rounded px-1 text-xs text-red-400 hover:bg-zinc-800" onClick={onDelete}>
          🗑
        </button>
      </span>
    </div>
  );
}

function CameraPanel({
  refCam,
  reference,
  live,
  fullscreen,
  onToggleFullscreen,
  globalSettings,
  override,
  onOverrideChange,
  grid,
  blinkOn,
  transport,
}: {
  refCam: ReferenceCamera;
  reference: Reference;
  live: Camera | null;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  globalSettings: { opacity: number; mode: OverlayMode; edges: boolean };
  override: Override | null;
  onOverrideChange: (o: Override | null) => void;
  grid: boolean;
  blinkOn: boolean;
  transport: Transport;
}) {
  const eff = override ?? {
    opacity: globalSettings.opacity,
    mode: globalSettings.mode,
    edges: globalSettings.edges,
  };
  const thumb = reference.thumb_urls?.[refCam.device_key] ?? null;
  const refSrc = thumb ? (eff.edges ? transport.edgesUrl(thumb) : thumb) : null;
  const showRef = eff.mode === 'blink' ? blinkOn : true;

  const body = (
    <div className="relative h-full w-full bg-black" onClick={fullscreen ? undefined : onToggleFullscreen}>
      {live ? (
        <CamPreview
          url={transport.previewUrl(live.cam_id, 960, 10)}
          alt={`${live.cam_id} live`}
          className="absolute inset-0 h-full w-full object-contain"
        />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center text-sm font-bold text-zinc-500">
          camera not currently selected
        </div>
      )}
      {eff.mode === 'side' ? (
        <div className="absolute inset-0 flex">
          <div className="h-full w-1/2" />
          <div className="h-full w-1/2 border-l border-zinc-700">
            {refSrc ? (
              <img
                src={refSrc}
                alt="reference still"
                data-testid={`overlay-${refCam.device_key}`}
                className="h-full w-full object-contain"
                style={{ opacity: eff.opacity / 100 }}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-zinc-500">no still</div>
            )}
          </div>
        </div>
      ) : (
        refSrc && (
          <img
            src={refSrc}
            alt="reference overlay"
            data-testid={`overlay-${refCam.device_key}`}
            className="absolute inset-0 h-full w-full object-contain"
            style={{
              opacity: eff.mode === 'blink' ? (showRef ? 1 : 0) : eff.opacity / 100,
              mixBlendMode: eff.mode === 'difference' ? 'difference' : 'normal',
            }}
          />
        )
      )}
      {grid && <GridOverlay />}
    </div>
  );

  const chrome = (
    <div className="flex items-center gap-2 bg-zinc-950 px-2 py-1 text-xs">
      <span className="font-black text-zinc-100">{live?.cam_id ?? refCam.cam_id_at_capture}</span>
      <span className="truncate text-zinc-500" title={refCam.label}>
        {refCam.label}
      </span>
      <span className="ml-auto">
        <VerdictBadge deviceKey={refCam.device_key} camId={live?.cam_id ?? refCam.cam_id_at_capture} fallback={live?.drift ?? null} />
      </span>
      <label className="flex items-center gap-1 text-[10px] font-bold text-zinc-500">
        <input
          type="checkbox"
          checked={override !== null}
          aria-label="Per-panel override"
          onChange={(e) =>
            onOverrideChange(
              e.target.checked
                ? { opacity: globalSettings.opacity, mode: globalSettings.mode, edges: globalSettings.edges }
                : null,
            )
          }
        />
        override
      </label>
    </div>
  );

  const panel = (
    <div className="flex min-h-0 flex-col overflow-hidden rounded border border-zinc-700">
      <div className="relative min-h-0 flex-1">{body}</div>
      {override && (
        <div className="flex items-center gap-2 bg-zinc-900 px-2 py-1 text-[11px]">
          <input
            type="range"
            min={0}
            max={100}
            value={override.opacity}
            aria-label="Panel overlay opacity"
            className="w-24"
            onChange={(e) => onOverrideChange({ ...override, opacity: Number(e.target.value) })}
          />
          <select
            value={override.mode}
            aria-label="Panel overlay mode"
            className="rounded bg-zinc-950 px-1 py-0.5"
            onChange={(e) => onOverrideChange({ ...override, mode: e.target.value as OverlayMode })}
          >
            {MODES.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={override.edges}
              aria-label="Panel edges only"
              onChange={(e) => onOverrideChange({ ...override, edges: e.target.checked })}
            />
            edges
          </label>
        </div>
      )}
      {chrome}
    </div>
  );

  if (fullscreen) {
    return (
      <div className="fixed inset-0 z-40 flex flex-col bg-zinc-950 p-2" data-testid="fullscreen-panel">
        <div className="mb-1 flex items-center gap-2">
          <span className="text-sm font-black">{live?.cam_id ?? refCam.cam_id_at_capture}</span>
          <button
            type="button"
            className="ml-auto rounded border border-zinc-600 px-3 py-1 text-sm font-bold hover:bg-zinc-800"
            onClick={onToggleFullscreen}
          >
            Exit full screen
          </button>
        </div>
        <div className="min-h-0 flex-1">{panel}</div>
      </div>
    );
  }
  return panel;
}

function GridOverlay() {
  return (
    <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden data-testid="grid-overlay">
      <line x1="33.33%" y1="0" x2="33.33%" y2="100%" stroke="rgba(255,255,255,0.35)" strokeWidth="1" />
      <line x1="66.66%" y1="0" x2="66.66%" y2="100%" stroke="rgba(255,255,255,0.35)" strokeWidth="1" />
      <line x1="0" y1="33.33%" x2="100%" y2="33.33%" stroke="rgba(255,255,255,0.35)" strokeWidth="1" />
      <line x1="0" y1="66.66%" x2="100%" y2="66.66%" stroke="rgba(255,255,255,0.35)" strokeWidth="1" />
      <line x1="50%" y1="42%" x2="50%" y2="58%" stroke="rgba(0,255,255,0.8)" strokeWidth="1.5" />
      <line x1="42%" y1="50%" x2="58%" y2="50%" stroke="rgba(0,255,255,0.8)" strokeWidth="1.5" />
    </svg>
  );
}
