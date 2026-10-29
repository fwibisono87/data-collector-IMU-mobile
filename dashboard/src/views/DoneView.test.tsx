import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { makeStep } from '../test/helpers';
import {
  makeConsolidation,
  makeSessionDetail,
  makeSnapshot,
  renderWithApp,
  stubTransport,
} from '../test/helpers';
import { DoneView } from './DoneView';

describe('Done view (SPEC §5.3)', () => {
  it('renders a PASS session with folder, ZIP and integrity devices', async () => {
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot() });
    await screen.findByTestId('done-view');
    await waitFor(() => expect(screen.getByTestId('verdict').textContent).toBe('PASS'));
    expect(screen.getByTestId('integrity-devices').textContent).toContain('chest');
    expect(screen.getByTestId('integrity-devices').textContent).toContain('PASS');
    expect(screen.getByTestId('open-folder')).toBeTruthy();
  });

  it('survives a partial integrity report (no devices key)', async () => {
    const transport = stubTransport({
      getSession: vi.fn(async () =>
        makeSessionDetail({
          verdict: 'PARTIAL',
          integrity_report: { session_id: 's-test-1', status: 'PARTIAL' }, // no devices
        }),
      ),
    });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });
    await waitFor(() => expect(screen.getByTestId('verdict').textContent).toBe('PARTIAL'));
    expect(screen.getByTestId('integrity').textContent).toContain('no per-device section');
  });

  it('survives a missing integrity report entirely', async () => {
    const transport = stubTransport({
      getSession: vi.fn(async () => makeSessionDetail({ integrity_report: null, verdict: 'UNKNOWN' })),
    });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });
    await screen.findByTestId('done-view');
    expect(screen.getByTestId('integrity').textContent).toContain('No integrity report yet');
  });

  it('Open folder 501 tells the operator to open it manually, with the path', async () => {
    const transport = stubTransport({
      openFolder: vi.fn(async () => {
        throw new ApiError(501, 'unsupported', 'Open folder is not supported on this backend');
      }),
    });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });
    await screen.findByTestId('open-folder');
    fireEvent.click(screen.getByTestId('open-folder'));
    await screen.findByTestId('open-folder-msg');
    expect(screen.getByTestId('open-folder-msg').textContent).toContain('open it manually');
    expect(screen.getByTestId('open-folder-msg').textContent).toContain('S012_baseline');
  });

  it('shows late rows and outstanding uploads inline', async () => {
    const transport = stubTransport({
      getSession: vi.fn(async () =>
        makeSessionDetail({
          verdict: 'PARTIAL',
          late: [{ device_id: 'dev-waist', role: 'waist', rows: 1204 }],
          uploads: [
            {
              device_id: 'dev-waist',
              role: 'waist',
              received_bytes: 1_284_000,
              total_bytes: 2_400_000,
              complete: false,
              verified: false,
            },
          ],
        }),
      ),
    });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });
    await screen.findByTestId('late-rows');
    expect(screen.getByTestId('late-rows').textContent).toContain('1,204 late rows');
    expect(screen.getByTestId('late-rows').textContent).toContain('rescue upload');
  });

  it('lists the session notices with Dismiss', async () => {
    const notice = {
      id: 'n1',
      level: 'warn' as const,
      code: 'late_data',
      message: 'waist: late rows merged',
      session_id: 's-test-1',
      created_at_ms: Date.now(),
      resolved: false,
    };
    const transport = stubTransport();
    renderWithApp(<DoneView sid="s-test-1" />, {
      snapshot: makeSnapshot({ notices: [notice] }),
      transport,
    });
    await screen.findByText('waist: late rows merged');
    fireEvent.click(screen.getByText('Dismiss'));
    await waitFor(() => expect(transport.dismissNotice).toHaveBeenCalledWith('n1'));
  });

  it('offers Consolidate now while phone data is pending, then shows Current', async () => {
    let row: 'pending_phone_data' | 'current' = 'pending_phone_data';
    const getSession = vi.fn(async () =>
      makeSessionDetail({
        verdict: 'PARTIAL',
        consolidation:
          row === 'current'
            ? makeConsolidation({
                state: 'current',
                detail: 'every source is consolidated',
                can_consolidate: false,
                recovery_pending: false,
                rescue_uploads: 0,
              })
            : makeConsolidation(),
      }),
    );
    const consolidate = vi.fn(async () => {
      row = 'current';
      return { zip_path: 's.zip' };
    });
    const transport = stubTransport({ getSession, consolidate });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });

    await waitFor(() =>
      expect(screen.getByTestId('consolidation-state').textContent).toBe('Pending phone data'),
    );
    expect(screen.getByTestId('consolidation-detail').textContent).toContain('1 rescue upload');
    const button = screen.getByTestId('consolidate-now') as HTMLButtonElement;
    expect(button.disabled).toBe(false);

    fireEvent.click(button);
    await waitFor(() => expect(consolidate).toHaveBeenCalledWith('s-test-1'));
    await waitFor(() =>
      expect(screen.getByTestId('consolidation-state').textContent).toBe('Current'),
    );
    expect((screen.getByTestId('consolidate-now') as HTMLButtonElement).disabled).toBe(true);
  });

  it('a current consolidation disables the button and a whole session says Not needed', async () => {
    const transport = stubTransport({
      getSession: vi.fn(async () =>
        makeSessionDetail({
          consolidation: makeConsolidation({
            state: 'not_needed',
            detail: 'session is whole',
            can_consolidate: false,
            whole: true,
            recovery_pending: false,
            rescue_uploads: 0,
          }),
        }),
      ),
    });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });
    await waitFor(() =>
      expect(screen.getByTestId('consolidation-state').textContent).toBe('Not needed'),
    );
    expect(screen.getByTestId('consolidation-detail').textContent).toBe('session is whole');
    expect((screen.getByTestId('consolidate-now') as HTMLButtonElement).disabled).toBe(true);
  });

  it('disables Consolidate now while the backend step is running', async () => {
    const transport = stubTransport({
      getSession: vi.fn(async () =>
        makeSessionDetail({
          consolidation: makeConsolidation(),
          finalize: [
            makeStep({ name: 'consolidate', status: 'running', detail: 'merging' }),
          ],
        }),
      ),
    });
    renderWithApp(<DoneView sid="s-test-1" />, { snapshot: makeSnapshot(), transport });
    await waitFor(() => expect(screen.getByTestId('consolidate-now').textContent).toContain('Consolidating'));
    expect((screen.getByTestId('consolidate-now') as HTMLButtonElement).disabled).toBe(true);
  });

  it('a 404 session says it is not on disk yet', async () => {
    const transport = stubTransport({
      getSession: vi.fn(async () => {
        throw new ApiError(404, 'not_found', 'Session s-missing not found');
      }),
    });
    renderWithApp(<DoneView sid="s-missing" />, { snapshot: makeSnapshot(), transport });
    await screen.findByText(/not found on disk yet/);
  });
});
