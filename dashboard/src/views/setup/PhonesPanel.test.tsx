import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makePhone, makeSnapshot, renderWithApp } from '../../test/helpers';
import { PhonesPanel } from './PhonesPanel';

/**
 * L7a §3.3: at 1024×768 the phone cards are narrow, and a long role name used to
 * squeeze the ONLINE badge until it clipped. The role truncates; the badge keeps
 * its full width (`shrink-0`, never wraps).
 */
describe('Phone cards at narrow widths (L7a §3.3)', () => {
  it('keeps the ONLINE badge unclipped and truncates the role instead', () => {
    renderWithApp(<PhonesPanel />, {
      snapshot: makeSnapshot({ phones: [makePhone({ role: 'thigh_left', device_id: 'dev-thigh' })] }),
    });

    const card = screen.getByTestId('phone-card-thigh_left');
    const badge = within(card).getByText('ONLINE');
    expect(badge.className).toContain('shrink-0');
    expect(badge.className).toContain('whitespace-nowrap');

    const role = within(card).getByText('thigh_left');
    expect(role.className).toContain('truncate');
    expect(role.className).toContain('min-w-0');
  });

  it('keeps the OFFLINE badge unclipped too, so the drop duration stays readable', () => {
    renderWithApp(<PhonesPanel />, {
      snapshot: makeSnapshot({
        phones: [
          makePhone({
            role: 'thigh_left',
            online: false,
            offline_since_ms: Date.now() - 42_000,
            telemetry_age_ms: null,
          }),
        ],
      }),
    });
    const badge = within(screen.getByTestId('phone-card-thigh_left')).getByText(/OFFLINE/);
    expect(badge.className).toContain('shrink-0');
    expect(badge.className).toContain('whitespace-nowrap');
  });
});