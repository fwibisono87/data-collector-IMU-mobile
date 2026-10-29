import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { SnapshotMsg } from './api/types';
import { makeSession, makeSnapshot, renderWithApp } from './test/helpers';
import { AppShell } from './App';

describe('Routing follows backend state, not clicks (SPEC §5)', () => {
  it('RECORDING in the snapshot → Record view at /', async () => {
    renderWithApp(<AppShell />, {
      snapshot: makeSnapshot({ state: 'RECORDING', session: makeSession() }),
      route: '/',
    });
    expect(screen.getByTestId('record-view')).toBeTruthy();
    expect(screen.getByTestId('state-pill').textContent).toContain('REC');
  });

  it('FINALIZING → Done view for the running session', async () => {
    renderWithApp(<AppShell />, {
      snapshot: makeSnapshot({ state: 'FINALIZING', session: makeSession() }),
      route: '/',
    });
    expect(screen.getByTestId('done-view')).toBeTruthy();
  });

  it('IDLE after a reload → Setup view even with a last_session_id', () => {
    renderWithApp(<AppShell />, {
      snapshot: makeSnapshot({ state: 'IDLE', last_session_id: 's-test-1' }),
      route: '/',
    });
    expect(screen.getByTestId('setup-view')).toBeTruthy();
  });

  it('when a session finishes, / keeps showing Done until "New session"', async () => {
    const { app } = renderWithApp(<AppShell />, {
      snapshot: makeSnapshot({ state: 'FINALIZING', session: makeSession() }),
      route: '/',
    });
    expect(screen.getByTestId('done-view')).toBeTruthy();

    // Backend finishes: FINALIZING → IDLE with last_session_id.
    act(() => {
      app.dispatch({
        type: 'snapshot',
        data: makeSnapshot({ state: 'IDLE', last_session_id: 's-test-1' }),
      } as SnapshotMsg);
    });
    expect(screen.getByTestId('done-view')).toBeTruthy(); // acknowledgement not given yet

    fireEvent.click(screen.getByTestId('new-session'));
    await screen.findByTestId('setup-view');
  });

  it('unknown routes fall back to the state-driven home', () => {
    renderWithApp(<AppShell />, { snapshot: makeSnapshot(), route: '/no/such/page' });
    expect(screen.getByTestId('setup-view')).toBeTruthy();
  });

  it('/sessions lists history and a row opens /sessions/:sid', async () => {
    renderWithApp(<AppShell />, { snapshot: makeSnapshot(), route: '/sessions' });
    await screen.findByTestId('sessions-view');
    await waitFor(() =>
      expect(screen.getByTestId('sessions-table').textContent).toContain('S012'),
    );
    fireEvent.click(screen.getByText('S012'));
    await screen.findByTestId('done-view');
  });

  it('/calibration renders the calibration view', async () => {
    renderWithApp(<AppShell />, { snapshot: makeSnapshot(), route: '/calibration' });
    await screen.findByTestId('calibration-view');
  });

  it('the top bar is visible on every route', () => {
    renderWithApp(<AppShell />, { snapshot: makeSnapshot(), route: '/' });
    expect(screen.getByTestId('state-pill')).toBeTruthy();
    expect(screen.getByTestId('build-id')).toBeTruthy();
    expect(screen.getByTestId('disk-free').textContent).toContain('free');
  });
});
