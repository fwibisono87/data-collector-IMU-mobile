import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/errors';
import { makeSnapshot, renderWithApp, stubTransport } from '../../test/helpers';
import { ReadinessPanel } from './ReadinessPanel';

describe('Readiness / START (SPEC §5.1)', () => {
  it('blockers disable START and are listed in red', () => {
    const snap = makeSnapshot({
      readiness: {
        blockers: [
          { code: 'no_phones', message: 'No phone online' },
          { code: 'draft_subject', message: 'Subject is required', subject: 'subject' },
        ],
        warnings: [],
      },
    });
    renderWithApp(<ReadinessPanel />, { snapshot: snap });
    const btn = screen.getByTestId('start-button') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(screen.getByTestId('blockers').textContent).toContain('No phone online');
    expect(screen.getByTestId('blockers').textContent).toContain('Subject is required');
    expect(screen.queryByTestId('warnings')).toBeNull();
  });

  it('warnings only → START stays enabled and reads "Start anyway"', () => {
    const snap = makeSnapshot({
      readiness: {
        blockers: [],
        warnings: [{ code: 'low_rate', message: 'thigh_left samples at 72 Hz' }],
      },
    });
    renderWithApp(<ReadinessPanel />, { snapshot: snap });
    const btn = screen.getByTestId('start-button') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe('Start anyway');
    expect(screen.getByTestId('warnings').textContent).toContain('72 Hz');
  });

  it('no issues → "Start session"', () => {
    renderWithApp(<ReadinessPanel />, { snapshot: makeSnapshot() });
    const btn = screen.getByTestId('start-button') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe('Start session');
  });

  it('click sends POST /api/session/start through the transport', async () => {
    const transport = stubTransport();
    renderWithApp(<ReadinessPanel />, { transport });
    fireEvent.click(screen.getByTestId('start-button'));
    await waitFor(() => expect(transport.startSession).toHaveBeenCalledTimes(1));
  });

  it('a 409 blocked from START shows the returned blockers inline', async () => {
    const transport = stubTransport({
      startSession: vi.fn(async () => {
        throw new ApiError(409, 'blocked', 'START refused — fix the blockers first', {
          blockers: [{ code: 'disk_space', message: 'Less than 5 GB free on D:' }],
        });
      }),
    });
    renderWithApp(<ReadinessPanel />, { transport });
    fireEvent.click(screen.getByTestId('start-button'));
    await screen.findByText(/Less than 5 GB free/);
    expect(screen.getByTestId('blockers').textContent).toContain('Less than 5 GB free');
  });

  it('a non-409 failure shows the message as an error', async () => {
    const transport = stubTransport({
      startSession: vi.fn(async () => {
        throw new ApiError(500, 'internal', 'backend exploded');
      }),
    });
    renderWithApp(<ReadinessPanel />, { transport });
    fireEvent.click(screen.getByTestId('start-button'));
    await screen.findByRole('alert');
    expect(screen.getByRole('alert').textContent).toContain('backend exploded');
  });
});
