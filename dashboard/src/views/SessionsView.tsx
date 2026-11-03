import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage } from '../api/errors';
import type { SessionSummary } from '../api/types';
import { PanelBoundary } from '../components/PanelBoundary';
import { EMPTY_ARR } from '../lib/empty';
import { fmtBytes, fmtClock } from '../lib/format';
import { useSnapshot, useTransport } from '../state/store';
import { NoticeRow } from './DoneView';

const VERDICT_STYLE: Record<string, string> = {
  PASS: 'text-green-300',
  PARTIAL: 'text-amber-300',
  FAIL: 'text-red-300',
  UNKNOWN: 'text-zinc-400',
  NONE: 'text-zinc-500',
};

/**
 * Sessions history (SPEC §5.4): every session found on disk, newest first,
 * paginated through `next`. A row opens the same Done view. Notices are
 * server-side records — dismissing one updates every browser (SPEC §6.2).
 */
export function SessionsView() {
  const transport = useTransport();
  const navigate = useNavigate();
  const [rows, setRows] = useState<SessionSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const notices = useSnapshot((s) => s.snapshot?.notices ?? EMPTY_ARR);

  const load = useCallback(
    async (before?: string) => {
      setLoading(true);
      try {
        const res = await transport.listSessions(50, before ?? null);
        setRows((prev) => (before ? [...prev, ...res.sessions] : res.sessions));
        setNext(res.next);
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setLoading(false);
      }
    },
    [transport],
  );

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="h-full overflow-y-auto p-3" data-testid="sessions-view">
      <div className="mx-auto flex max-w-5xl flex-col gap-3">
        <PanelBoundary name="Notices" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">
            Notices <span className="text-zinc-500">({notices.length})</span>
          </h2>
          {notices.length === 0 ? (
            <p className="text-sm text-zinc-500">Nothing outstanding.</p>
          ) : (
            <ul className="space-y-1">
              {notices.map((n) => (
                <NoticeRow
                  key={n.id}
                  notice={n}
                  onDismiss={() => transport.dismissNotice(n.id).catch((e) => setError(errorMessage(e)))}
                />
              ))}
            </ul>
          )}
        </PanelBoundary>

        <PanelBoundary name="Sessions table" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <h2 className="mb-2 text-sm font-black uppercase tracking-wider text-zinc-400">Sessions</h2>
          {error && <p className="mb-2 text-sm font-semibold text-red-400" role="alert">{error}</p>}
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-zinc-700 text-xs uppercase tracking-wide text-zinc-500">
                <th className="py-1 pr-2">Date</th>
                <th className="py-1 pr-2">Subject</th>
                <th className="py-1 pr-2">Tag</th>
                <th className="py-1 pr-2">Duration</th>
                <th className="py-1 pr-2">Phones</th>
                <th className="py-1 pr-2">Cams</th>
                <th className="py-1 pr-2">Verdict</th>
                <th className="py-1 pr-2">ZIP</th>
                <th className="py-1 pr-2">Uploads</th>
              </tr>
            </thead>
            <tbody data-testid="sessions-table">
              {rows.map((s) => (
                <tr
                  key={s.session_id}
                  className="cursor-pointer border-b border-zinc-800/70 hover:bg-zinc-800/50"
                  onClick={() => navigate(`/sessions/${encodeURIComponent(s.session_id)}`)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') navigate(`/sessions/${encodeURIComponent(s.session_id)}`);
                  }}
                >
                  <td className="py-1.5 pr-2 tabular-nums text-zinc-400">{fmtClock(s.started_at_ms)}</td>
                  <td className="py-1.5 pr-2 font-bold text-zinc-100">{s.subject || '—'}</td>
                  <td className="py-1.5 pr-2 text-zinc-300">{s.session_tag || '—'}</td>
                  <td className="py-1.5 pr-2 tabular-nums">{s.duration_s != null ? `${s.duration_s.toFixed(0)} s` : '—'}</td>
                  <td className="py-1.5 pr-2 tabular-nums">{s.phones.length}</td>
                  <td className="py-1.5 pr-2 tabular-nums">{s.cameras.length}</td>
                  <td className={`py-1.5 pr-2 font-black ${VERDICT_STYLE[s.verdict] ?? 'text-zinc-400'}`}>{s.verdict}</td>
                  <td className="py-1.5 pr-2 tabular-nums text-zinc-400">
                    {s.zip_path ? (s.zip_bytes != null ? fmtBytes(s.zip_bytes) : '✓') : '—'}
                  </td>
                  <td className="py-1.5 pr-2 tabular-nums">
                    {s.outstanding_uploads > 0 ? (
                      <span className="font-bold text-amber-300">{s.outstanding_uploads} open</span>
                    ) : (
                      <span className="text-zinc-600">0</span>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && !loading && (
                <tr>
                  <td colSpan={9} className="py-3 text-zinc-500">
                    No sessions on disk yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <div className="mt-2 flex items-center gap-2">
            {next && (
              <button
                type="button"
                disabled={loading}
                className="rounded border border-zinc-700 px-3 py-1 text-sm font-bold hover:bg-zinc-800 disabled:opacity-40"
                onClick={() => void load(next)}
              >
                Load older sessions
              </button>
            )}
            {loading && <span className="text-xs text-zinc-500">loading…</span>}
          </div>
        </PanelBoundary>
      </div>
    </div>
  );
}
