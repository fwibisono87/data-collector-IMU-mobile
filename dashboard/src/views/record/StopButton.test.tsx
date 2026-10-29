import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { makeSession, makeSnapshot, renderWithApp, stubTransport } from '../../test/helpers';
import { StopButton } from './StopButton';

function recordingSnapshot() {
  return makeSnapshot({ state: 'RECORDING', session: makeSession() });
}

async function hold(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('STOP press-and-hold (SPEC §5.2)', () => {
  it('releasing before 1 s sends nothing', async () => {
    const transport = stubTransport();
    renderWithApp(<StopButton />, { snapshot: recordingSnapshot(), transport });
    const btn = screen.getByTestId('stop-button');
    fireEvent.pointerDown(btn);
    await hold(600);
    fireEvent.pointerUp(btn);
    expect(transport.stopSession).not.toHaveBeenCalled();
    expect(screen.getByTestId('stop-fill').style.width).toBe('0%'); // fill reset
  });

  it('a full 1 s hold sends exactly one stop', async () => {
    const transport = stubTransport();
    renderWithApp(<StopButton />, { snapshot: recordingSnapshot(), transport });
    const btn = screen.getByTestId('stop-button');
    fireEvent.pointerDown(btn);
    await hold(1100);
    fireEvent.pointerUp(btn);
    expect(transport.stopSession).toHaveBeenCalledTimes(1);
  });

  it('a double hold does not send twice', async () => {
    const transport = stubTransport();
    renderWithApp(<StopButton />, { snapshot: recordingSnapshot(), transport });
    const btn = screen.getByTestId('stop-button');
    fireEvent.pointerDown(btn);
    await hold(1100);
    fireEvent.pointerUp(btn);
    expect(transport.stopSession).toHaveBeenCalledTimes(1);
    // Second hold attempt after completion: disarmed, must not re-send.
    fireEvent.pointerDown(btn);
    await hold(2000);
    fireEvent.pointerUp(btn);
    expect(transport.stopSession).toHaveBeenCalledTimes(1);
  });

  it('holding Space stops too; releasing Space early does nothing', async () => {
    const transport = stubTransport();
    renderWithApp(<StopButton />, { snapshot: recordingSnapshot(), transport });
    fireEvent.keyDown(window, { code: 'Space', key: ' ' });
    await hold(400);
    fireEvent.keyUp(window, { code: 'Space', key: ' ' });
    expect(transport.stopSession).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { code: 'Space', key: ' ' });
    await hold(1100);
    fireEvent.keyUp(window, { code: 'Space', key: ' ' });
    expect(transport.stopSession).toHaveBeenCalledTimes(1);
  });

  it('a refused stop re-arms the button and shows why', async () => {
    const stop = vi.fn(async (): Promise<{ session_id: string }> => {
      throw new Error('Not recording — STOP refused');
    });
    const transport = stubTransport({ stopSession: stop });
    renderWithApp(<StopButton />, { snapshot: recordingSnapshot(), transport });
    const btn = screen.getByTestId('stop-button');
    fireEvent.pointerDown(btn);
    await hold(1100);
    await act(async () => {}); // flush the rejection microtask
    expect(screen.getByTestId('stop-error').textContent).toContain('STOP refused');
    // Re-armed: a successful hold afterwards still works.
    stop.mockResolvedValueOnce({ session_id: 's1' });
    fireEvent.pointerDown(btn);
    await hold(1100);
    expect(stop).toHaveBeenCalledTimes(2);
  });
});
