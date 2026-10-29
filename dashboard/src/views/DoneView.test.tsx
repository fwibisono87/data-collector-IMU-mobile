import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { makeSessionDetail, makeSnapshot, renderWithApp, stubTransport } from '../test/helpers';
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
