import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithApp, stubTransport } from '../../test/helpers';
import { CamerasPanel } from './CamerasPanel';

/**
 * L7a §3.4: the device checklist used to be fetched once on mount, so a camera
 * plugged in after the page loaded never appeared. Setup re-enumerates every 5 s
 * while it is visible, pauses in a hidden tab, and has a manual Refresh.
 */

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
});

function devices(labels: string[]) {
  return {
    devices: labels.map((label, i) => ({
      device_key: `fake:${i}`,
      label,
      backend: 'fake' as const,
    })),
  };
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

describe('Cameras panel device list (L7a §3.4)', () => {
  it('re-fetches every 5 s while visible so a hot-plugged camera appears', async () => {
    vi.useFakeTimers();
    const list = vi
      .fn()
      .mockResolvedValueOnce(devices(['Integrated Camera']))
      .mockResolvedValue(devices(['Integrated Camera', 'USB 2.0 Camera']));
    const transport = stubTransport({ listCameraDevices: list });

    renderWithApp(<CamerasPanel />, { transport });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(list).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Integrated Camera')).toBeTruthy();
    expect(screen.queryByText('USB 2.0 Camera')).toBeNull();

    // The camera is plugged in; five seconds later it is in the checklist.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText('USB 2.0 Camera')).toBeTruthy();
  });

  it('pauses while the tab is hidden and catches up when it returns', async () => {
    vi.useFakeTimers();
    const list = vi.fn(async () => devices(['Integrated Camera']));
    const transport = stubTransport({ listCameraDevices: list });

    renderWithApp(<CamerasPanel />, { transport });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(list).toHaveBeenCalledTimes(1);

    setVisibility('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(list).toHaveBeenCalledTimes(1);

    setVisibility('visible');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('has a Refresh button for an operator who does not want to wait', async () => {
    vi.useFakeTimers();
    const list = vi.fn(async () => devices(['Integrated Camera']));
    const transport = stubTransport({ listCameraDevices: list });

    renderWithApp(<CamerasPanel />, { transport });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(list).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('refresh-cameras'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(list).toHaveBeenCalledTimes(2);
  });
});