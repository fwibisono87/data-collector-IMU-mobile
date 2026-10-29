import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage, isApiError } from '../api/errors';
import type { ConsolidationStatus, FinalizeStep, Notice, SessionDetail } from '../api/types';
import { PanelBoundary } from '../components/PanelBoundary';
import { EMPTY_ARR } from '../lib/empty';
import { fmtBytes, fmtClock, fmtNumber, fmtSeconds } from '../lib/format';
import { useSnapshot, useTransport } from '../state/store';

const STEP_ICON: Record<FinalizeStep['status'], string> = {
  pending: '○',
  running: '◐',
  done: '✓',
  failed: '✗',
  skipped: '–',
};

const STEP_STYLE: Record<FinalizeStep['status'], string> = {
  pending: 'text-zinc-500',
  running: 'text-amber-300 animate-pulse',
  done: 'text-green-400',
  failed: 'text-red-400 font-bold',
  skipped: 'text-zinc-600',
};

const VERDICT_STYLE: Record<string, string> = {
  PASS: 'bg-green-500/15 text-green-300 border-green-500/50',
  PARTIAL: 'bg-amber-500/15 text-amber-300 border-amber-500/50',
  FAIL: 'bg-red-500/15 text-red-300 border-red-500/50',
  UNKNOWN: 'bg-zinc-500/15 text-zinc-300 border-zinc-500/50',
  NONE: 'bg-zinc-500/15 text-zinc-300 border-zinc-500/50',
};

/**
 * Done view (SPEC §5.3) for any session id — the running one while it
 * finalises, `last_session_id` after IDLE, or any row from the history.
 * Everything comes from the backend's per-session record; it survives reload
 * and is identical in every browser. The operator is never asked to download
 * anything: the ZIP is already on disk.
 */
export function DoneView({ sid, onNewSession }: { sid: string; onNewSession?: () => void }) {
  const transport = useTransport();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openFolderMsg, setOpenFolderMsg] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [consolidating, setConsolidating] = useState(false);

  // Live finalize progress from the snapshot when it is about THIS session.
  const liveFinalize = useSnapshot((s) =>
    s.snapshot?.finalize && s.snapshot.finalize.session_id === sid ? s.snapshot.finalize : null,
  );
  const liveSig = JSON.stringify(liveFinalize?.steps ?? null);
  const notices = useSnapshot((s) => s.snapshot?.notices ?? EMPTY_ARR);
  const sessionNotices = notices.filter((n) => n.session_id === sid);

  const load = useCallback(() => {
    transport
      .getSession(sid)
      .then((d) => {
        setDetail(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(isApiError(e) && e.status === 404 ? `Session ${sid} not found on disk yet.` : errorMessage(e)));
  }, [transport, sid]);

  // Refresh every 5 s while open, and immediately when a finalize message moves a step.
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(load, [load, liveSig]);

  const steps = liveFinalize?.steps ?? detail?.finalize ?? [];
  const verdict = String(safeReport(detail)?.['status'] ?? detail?.verdict ?? 'UNKNOWN');

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied((c) => (c === what ? null : c)), 1500);
    } catch {
      setActionError('Clipboard unavailable — select the path and copy manually.');
    }
  };

  const openFolder = () => {
    setOpenFolderMsg(null);
    setActionError(null);
    transport.openFolder(sid).catch((e) => {
      if (isApiError(e) && e.status === 501) {
        setOpenFolderMsg(`Cannot open the folder from here — open it manually: ${detail?.folder ?? 'the session folder'}`);
      } else {
        setActionError(errorMessage(e));
      }
    });
  };

  const dismiss = (id: string) => {
    transport.dismissNotice(id).catch((e) => setActionError(errorMessage(e)));
  };

  const consolidateNow = () => {
    setConsolidating(true);
    setActionError(null);
    transport
      .consolidate(sid)
      .then(() => load())
      .catch((e) => setActionError(errorMessage(e)))
      .finally(() => setConsolidating(false));
  };

  return (
    <div className="h-full overflow-y-auto p-3" data-testid="done-view">
      <div className="mx-auto flex max-w-4xl flex-col gap-3">
        <PanelBoundary name="Done header" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-black">
              {detail ? `${detail.subject || '?'} · ${detail.session_tag || '?'}` : `Session ${sid}`}
            </h1>
            <span
              className={`rounded border px-2 py-1 text-sm font-black ${VERDICT_STYLE[verdict] ?? VERDICT_STYLE.UNKNOWN}`}
              data-testid="verdict"
            >
              {verdict}
            </span>
            {detail?.duration_s != null && (
              <span className="text-sm tabular-nums text-zinc-400">
                {detail.duration_s.toFixed(1)} s · started {fmtClock(detail.started_at_ms)}
              </span>
            )}
            <button
              type="button"
              data-testid="new-session"
              className="ml-auto rounded-lg bg-cyan-600 px-4 py-2 text-base font-black text-white hover:bg-cyan-500"
              onClick={() => {
                if (onNewSession) onNewSession();
                else navigate('/');
              }}
            >
              New session
            </button>
          </div>
          <p className="mt-1 font-mono text-xs text-zinc-500">{sid}</p>
          {loadError && <p className="mt-1 text-sm font-semibold text-amber-300">{loadError}</p>}
          {actionError && <p className="mt-1 text-sm font-semibold text-red-400">{actionError}</p>}
        </PanelBoundary>

        <PanelBoundary name="Finalize steps" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">Finalize</h2>
          {steps.length === 0 && <p className="text-sm text-zinc-500">Waiting for finalize progress…</p>}
          <ol className="space-y-1" data-testid="finalize-steps">
            {steps.map((s, i) => (
              <li key={`${s.name}-${i}`} className="flex items-baseline gap-2 text-sm">
                <span className={`w-5 text-center font-black ${STEP_STYLE[s.status]}`}>{STEP_ICON[s.status] ?? '?'}</span>
                <span className="w-32 font-bold text-zinc-200">{s.name}</span>
                <span className="w-16 tabular-nums text-zinc-500">
                  {s.status === 'done' || s.status === 'failed'
                    ? fmtSeconds((s.ended_at_ms ?? 0) - (s.started_at_ms ?? 0))
                    : s.status}
                </span>
                {s.detail && (
                  <span className={`min-w-0 flex-1 truncate ${s.status === 'failed' ? 'font-semibold text-red-400' : 'text-zinc-400'}`} title={s.detail}>
                    {s.detail}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </PanelBoundary>

        <PanelBoundary name="Consolidation" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <ConsolidationRow
            consolidation={detail?.consolidation ?? null}
            step={steps.find((s) => s.name === 'consolidate') ?? null}
            busy={consolidating}
            onConsolidate={consolidateNow}
          />
        </PanelBoundary>

        <PanelBoundary name="Integrity" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <IntegritySection detail={detail} verdict={verdict} />
        </PanelBoundary>

        <PanelBoundary name="Data location" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">Where the data is</h2>
          {detail && (
            <div className="space-y-2 text-sm">
              <PathRow label="Session folder" path={detail.folder} onCopy={() => copy(detail.folder, 'folder')} copied={copied === 'folder'} />
              {detail.zip_path ? (
                <>
                  <PathRow label="ZIP" path={detail.zip_path} onCopy={() => copy(detail.zip_path as string, 'zip')} copied={copied === 'zip'} />
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      data-testid="open-folder"
                      className="rounded border border-zinc-600 px-3 py-1.5 font-bold hover:bg-zinc-800"
                      onClick={openFolder}
                    >
                      Open folder
                    </button>
                    <a
                      href={transport.zipUrl(sid)}
                      className="rounded border border-zinc-700 px-3 py-1.5 font-semibold text-zinc-300 hover:bg-zinc-800"
                      download
                    >
                      Download ZIP{detail.zip_bytes != null ? ` (${fmtBytes(detail.zip_bytes)})` : ''}
                    </a>
                    <span className="text-xs text-zinc-500">downloading is never required — the ZIP is on disk</span>
                  </div>
                  {openFolderMsg && (
                    <p className="text-sm font-semibold text-amber-300" role="status" data-testid="open-folder-msg">
                      {openFolderMsg}
                    </p>
                  )}
                </>
              ) : (
                <p className="text-zinc-500">ZIP not written yet.</p>
              )}
            </div>
          )}
        </PanelBoundary>

        <PanelBoundary name="Late data" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">Late data &amp; uploads</h2>
          {!detail || (detail.late.length === 0 && detail.uploads.length === 0) ? (
            <p className="text-sm text-zinc-500">No late phone data or rescue uploads for this session.</p>
          ) : (
            <ul className="space-y-1 text-sm" data-testid="late-rows">
              {detail.late.map((l, i) => (
                <li key={`late-${l.device_id}-${i}`} className="tabular-nums text-zinc-300">
                  <span className="font-bold">{l.role}</span>: {fmtNumber(l.rows)} late rows merged
                </li>
              ))}
              {detail.uploads.map((u, i) => (
                <li key={`up-${u.device_id}-${i}`} className="tabular-nums text-zinc-300">
                  <span className="font-bold">{u.role}</span>: rescue upload {fmtBytes(u.received_bytes)}
                  {u.total_bytes != null ? ` / ${fmtBytes(u.total_bytes)}` : ''}
                  {u.complete ? (u.verified ? ' · verified ✓' : ' · complete') : ' · receiving…'}
                </li>
              ))}
            </ul>
          )}
        </PanelBoundary>

        <PanelBoundary name="Session notices" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">Notices</h2>
          {sessionNotices.length === 0 ? (
            <p className="text-sm text-zinc-500">No notices for this session.</p>
          ) : (
            <ul className="space-y-1">
              {sessionNotices.map((n) => (
                <NoticeRow key={n.id} notice={n} onDismiss={() => dismiss(n.id)} />
              ))}
            </ul>
          )}
        </PanelBoundary>
      </div>
    </div>
  );
}

const CONSOLIDATION_LABEL: Record<ConsolidationStatus['state'], { text: string; className: string }> = {
  current: {
    text: 'Current',
    className: 'bg-green-500/15 text-green-300 border-green-500/50',
  },
  pending_phone_data: {
    text: 'Pending phone data',
    className: 'bg-amber-500/15 text-amber-300 border-amber-500/50',
  },
  not_needed: {
    text: 'Not needed',
    className: 'bg-zinc-500/15 text-zinc-300 border-zinc-500/50',
  },
};

/**
 * The Done view's Consolidation row (L7a §2). It answers the one question the
 * step list cannot: does the ZIP on disk already contain every late row and
 * phone rescue upload? "Consolidate now" is the backend's own POST; it is
 * offered exactly when the backend says the merge is not current.
 */
function ConsolidationRow({
  consolidation,
  step,
  busy,
  onConsolidate,
}: {
  consolidation: ConsolidationStatus | null;
  step: FinalizeStep | null;
  busy: boolean;
  onConsolidate: () => void;
}) {
  if (!consolidation) {
    return (
      <>
        <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">Consolidation</h2>
        <p className="text-sm text-zinc-500">Consolidation status is not available for this session yet.</p>
      </>
    );
  }
  const running = busy || step?.status === 'running';
  const label = CONSOLIDATION_LABEL[consolidation.state];
  const disabled = running || !consolidation.can_consolidate;
  return (
    <div data-testid="consolidation-row">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-black uppercase tracking-wider text-zinc-400">Consolidation</h2>
        <span
          className={`rounded border px-2 py-0.5 text-xs font-black ${label?.className ?? CONSOLIDATION_LABEL.not_needed.className}`}
          data-testid="consolidation-state"
        >
          {label?.text ?? consolidation.state}
        </span>
        <button
          type="button"
          data-testid="consolidate-now"
          disabled={disabled}
          title={
            running
              ? 'A consolidation or package run is already in progress'
              : consolidation.can_consolidate
                ? 'Merge late rows and phone rescue uploads into the session ZIP now'
                : 'Nothing is waiting to be merged'
          }
          className="ml-auto rounded border border-cyan-500/60 px-3 py-1 text-xs font-black text-cyan-300 hover:bg-cyan-500/10 disabled:cursor-not-allowed disabled:border-zinc-700 disabled:text-zinc-500 disabled:hover:bg-transparent"
          onClick={onConsolidate}
        >
          {running ? 'Consolidating…' : 'Consolidate now'}
        </button>
      </div>
      <p className="mt-1 text-sm text-zinc-400" data-testid="consolidation-detail">
        {consolidation.detail}
      </p>
    </div>
  );
}

function PathRow({ label, path, onCopy, copied }: { label: string; path: string; onCopy: () => void; copied: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-28 shrink-0 text-xs font-bold uppercase text-zinc-500">{label}</span>
      <code className="min-w-0 flex-1 truncate rounded bg-zinc-950 px-2 py-1 font-mono text-xs text-zinc-200" title={path}>
        {path}
      </code>
      <button type="button" onClick={onCopy} className="rounded border border-zinc-700 px-2 py-1 text-xs font-bold hover:bg-zinc-800">
        {copied ? 'Copied ✓' : 'Copy'}
      </button>
    </div>
  );
}

export function NoticeRow({ notice, onDismiss }: { notice: Notice; onDismiss: () => void }) {
  const style =
    notice.level === 'error'
      ? 'border-red-500/50 bg-red-950/40 text-red-200'
      : notice.level === 'warn'
        ? 'border-amber-500/50 bg-amber-950/30 text-amber-200'
        : 'border-zinc-700 bg-zinc-950 text-zinc-300';
  return (
    <li className={`flex items-center gap-2 rounded border px-2 py-1 text-sm ${style}`}>
      <span className="min-w-0 flex-1">{notice.message}</span>
      <span className="text-[10px] text-zinc-500">{fmtClock(notice.created_at_ms)}</span>
      <button
        type="button"
        onClick={onDismiss}
        className="rounded border border-zinc-600 px-2 py-0.5 text-xs font-bold hover:bg-zinc-800"
      >
        Dismiss
      </button>
    </li>
  );
}

/**
 * The integrity report is an opaque object in the contract (it can be
 * partial, old, or absent). Read it defensively — a missing key must never
 * crash the view that tells the operator their data is safe.
 */
function safeReport(detail: SessionDetail | null): Record<string, unknown> | null {
  const r = detail?.integrity_report;
  return r && typeof r === 'object' ? (r as Record<string, unknown>) : null;
}

function IntegritySection({ detail, verdict }: { detail: SessionDetail | null; verdict: string }) {
  const report = safeReport(detail);
  const devicesRaw = report?.['devices'];
  const devices: Array<Record<string, unknown>> = Array.isArray(devicesRaw)
    ? (devicesRaw as Array<Record<string, unknown>>)
    : [];
  const reasons = report?.['analysis_ready_reasons'];
  return (
    <div data-testid="integrity">
      <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">
        Integrity <span className="font-black text-zinc-200">{verdict}</span>
      </h2>
      {!detail && <p className="text-sm text-zinc-500">Loading session record…</p>}
      {detail && !report && (
        <p className="text-sm text-zinc-500">
          No integrity report yet — validation runs as part of finalize.
        </p>
      )}
      {report && devices.length === 0 && (
        <p className="mb-2 rounded border border-amber-500/40 bg-amber-950/30 p-2 text-sm text-amber-200">
          This report has no per-device section (it may be partial). The session data is still
          written to disk — <code>tools/analyze_session.py</code> can re-check the folder.
        </p>
      )}
      {Array.isArray(reasons) && reasons.length > 0 && (
        <ul className="mb-2 list-inside list-disc text-xs text-amber-200/90">
          {reasons.slice(0, 8).map((r, i) => (
            <li key={i}>{String(r)}</li>
          ))}
        </ul>
      )}
      <ul className="space-y-1" data-testid="integrity-devices">
        {devices.map((d, i) => {
          const status = String(d['status'] ?? '?');
          const role = String(d['role'] ?? d['device_id'] ?? `device ${i + 1}`);
          return (
            <li key={String(d['device_id'] ?? i)} className="rounded bg-zinc-950 px-2 py-1 text-sm">
              <div className="flex items-baseline gap-2">
                <span className="font-bold text-zinc-200">{role}</span>
                <span className={status === 'PASS' ? 'font-black text-green-400' : 'font-black text-red-400'}>{status}</span>
                {typeof d['row_count'] === 'number' && (
                  <span className="tabular-nums text-zinc-500">{fmtNumber(d['row_count'])} rows</span>
                )}
              </div>
              {d['issue'] != null && <p className="text-xs font-semibold text-red-400">⚠ {String(d['issue'])}</p>}
              {typeof d['offline_total_ms'] === 'number' && d['offline_total_ms'] > 0 && (
                <p className="text-xs text-amber-300">
                  offline {fmtSeconds(d['offline_total_ms'])} in {String(d['offline_interval_count'] ?? '?')} intervals
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
