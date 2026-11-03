import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DriftMsg } from '../api/types';
import { VerdictBadge, shiftArrow } from '../components/VerdictBadge';
import { makeCam, makeReference, makeSnapshot, renderWithApp, stubTransport } from '../test/helpers';
import { CalibrationView } from './CalibrationView';

const THUMB = '/api/calibration/references/ref-test-1/fake_0.jpg';

function setup(over: Parameters<typeof stubTransport>[0] = {}) {
  const transport = stubTransport({
    listReferences: vi.fn(async () => ({
      references: [makeReference()],
      active_ref_id: 'ref-test-1',
    })),
    ...over,
  });
  const snap = makeSnapshot({
    cameras: [makeCam({ drift: null })],
    calibration: { active_ref_id: 'ref-test-1' },
  });
  return renderWithApp(<CalibrationView />, { transport, snapshot: snap });
}

beforeEach(() => {
  localStorage.clear(); // overlay prefs persist across tests otherwise
});

describe('Calibration view (CALIBRATION.md UI)', () => {
  it('sends calibration.watch on mount and unwatch on unmount', async () => {
    const { transport, unmount } = setup();
    await waitFor(() => expect(transport.watchCalibration).toHaveBeenCalled());
    // After the reference list lands, the watch is restarted for the picked ref.
    await waitFor(() => expect(transport.watchCalibration).toHaveBeenCalledWith('ref-test-1'));
    unmount();
    expect(transport.unwatchCalibration).toHaveBeenCalled();
  });

  it('shows the reference still as an overlay with the default 50 % opacity', async () => {
    setup();
    const overlay = (await screen.findByTestId('overlay-fake:0')) as HTMLImageElement;
    expect(overlay.src).toContain(THUMB);
    expect(overlay.style.opacity).toBe('0.5');
    expect(overlay.style.mixBlendMode).toBe('normal');
  });

  it('mode switching: Difference sets mix-blend-mode, Blend does not', async () => {
    setup();
    const overlay = (await screen.findByTestId('overlay-fake:0')) as HTMLImageElement;
    fireEvent.click(screen.getByTestId('mode-difference'));
    expect(overlay.style.mixBlendMode).toBe('difference');
    fireEvent.click(screen.getByTestId('mode-blend'));
    expect(overlay.style.mixBlendMode).toBe('normal');
    // Side by side renders the still beside the live view (no absolute overlay).
    fireEvent.click(screen.getByTestId('mode-side'));
    expect((screen.getByTestId('overlay-fake:0') as HTMLImageElement).style.opacity).toBe('0.5');
  });

  it('the opacity slider is applied to the overlay and persisted', async () => {
    setup();
    const overlay = (await screen.findByTestId('overlay-fake:0')) as HTMLImageElement;
    fireEvent.change(screen.getByTestId('opacity-slider'), { target: { value: '25' } });
    expect(overlay.style.opacity).toBe('0.25');
    expect(localStorage.getItem('imu.view.overlayOpacity')).toBe('25');
  });

  it('"Edges only" swaps the overlay to the edges.png companion', async () => {
    setup();
    const overlay = (await screen.findByTestId('overlay-fake:0')) as HTMLImageElement;
    expect(overlay.src).not.toContain('edges.png');
    fireEvent.click(screen.getByTestId('edges-toggle'));
    expect(overlay.src).toContain('/fake_0.edges.png');
  });

  it('grid toggle shows the crosshair overlay', async () => {
    setup();
    await screen.findByTestId('overlay-fake:0');
    expect(screen.queryByTestId('grid-overlay')).toBeNull();
    fireEvent.click(screen.getByTestId('grid-toggle'));
    expect(screen.getByTestId('grid-overlay')).toBeTruthy();
  });
});

describe('Verdict badge', () => {
  const base = { dx_px: 0, dy_px: 0, shift_pct: 0, score: 0.9, confidence: 0.9, at_ms: 0 };

  it.each([
    ['OK', 'OK'],
    ['CHECK', 'CHECK'],
    ['MOVED', 'MOVED'],
    ['NO_SIGNAL', 'NO SIGNAL'],
    ['MISSING', 'MISSING'],
    ['RESOLUTION_CHANGED', 'RESOLUTION CHANGED'],
  ] as const)('shows %s as "%s"', (verdict, label) => {
    renderWithApp(<VerdictBadge deviceKey="fake:0" fallback={{ ...base, verdict }} />, {
      snapshot: makeSnapshot(),
    });
    expect(screen.getByTestId('verdict-badge').textContent).toContain(label);
  });

  it('arrow points in the direction of the shift', () => {
    expect(shiftArrow(14.2, -3.4)).toBe('↗');
    expect(shiftArrow(-9, 2)).toBe('↙');
    expect(shiftArrow(0.2, -0.1)).toBe('·'); // sub-pixel: no direction
  });

  it('updates from the isolated drift store without a new snapshot', () => {
    const { app } = renderWithApp(<VerdictBadge deviceKey="fake:0" camId="cam1" fallback={null} />, {
      snapshot: makeSnapshot(),
    });
    expect(screen.queryByTestId('verdict-badge')).toBeNull();
    act(() => {
      app.dispatch({
        type: 'calibration.drift',
        data: {
          ref_id: 'ref-test-1',
          at_ms: Date.now(),
          cameras: [
            {
              ...base,
              verdict: 'MOVED',
              dx_px: 14.2,
              dy_px: -3.4,
              device_key: 'fake:0',
              cam_id: 'cam1',
            },
          ],
        },
      } as DriftMsg);
    });
    const badge = screen.getByTestId('verdict-badge');
    expect(badge.textContent).toContain('MOVED');
    expect(badge.textContent).toContain('Δx 14');
    expect(badge.textContent).toContain('↗');
  });
});
