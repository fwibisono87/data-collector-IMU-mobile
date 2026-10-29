import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { LabelResponse } from '../../api/types';
import { makeSession, makeSnapshot, renderWithApp, stubTransport } from '../../test/helpers';
import { LabelPanel } from './LabelPanel';

function recordingSnapshot() {
  return makeSnapshot({
    state: 'RECORDING',
    session: makeSession({ label: { id: 0, name: '0', since_ms: Date.now() } }),
  });
}

function key(k: string, target: Window | Element = window) {
  fireEvent.keyDown(target, { key: k });
}

describe('Label panel (SPEC §5.2)', () => {
  it('digits accumulate in the on-screen buffer, max 2 digits', () => {
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot() });
    expect(screen.getByTestId('label-buffer').textContent).toBe('—');
    key('1');
    key('2');
    expect(screen.getByTestId('label-buffer').textContent).toBe('12');
    key('3'); // third digit ignored
    expect(screen.getByTestId('label-buffer').textContent).toBe('12');
  });

  it('Backspace edits the buffer, Enter applies it and clears it', async () => {
    const transport = stubTransport();
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    key('1');
    key('2');
    key('Backspace');
    expect(screen.getByTestId('label-buffer').textContent).toBe('1');
    key('Enter');
    await waitFor(() => expect(transport.setLabel).toHaveBeenCalledWith(1));
    expect(screen.getByTestId('label-buffer').textContent).toBe('—');
  });

  it('Enter with an empty buffer does nothing', async () => {
    const transport = stubTransport();
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    key('Enter');
    expect(transport.setLabel).not.toHaveBeenCalled();
  });

  it('Escape returns to baseline label 0', async () => {
    const transport = stubTransport();
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    key('4');
    key('Escape');
    expect(screen.getByTestId('label-buffer').textContent).toBe('—');
    await waitFor(() => expect(transport.setLabel).toHaveBeenCalledWith(0));
  });

  it('out-of-range buffer shows an inline error and sends nothing', () => {
    const transport = stubTransport();
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    key('5');
    key('1'); // 51 > 50
    key('Enter');
    expect(screen.getByTestId('label-error').textContent).toContain('0–50');
    expect(transport.setLabel).not.toHaveBeenCalled();
  });

  it('shortcuts are disabled while a text input is focused', async () => {
    const transport = stubTransport();
    renderWithApp(
      <>
        <input aria-label="Subject" />
        <LabelPanel />
      </>,
      { snapshot: recordingSnapshot(), transport },
    );
    const input = screen.getByLabelText('Subject');
    fireEvent.keyDown(input, { key: '9' });
    expect(screen.getByTestId('label-buffer').textContent).toBe('—');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(transport.setLabel).not.toHaveBeenCalled();
  });

  it('shows a pending state until the REST call resolves', async () => {
    let resolveLabel: (v: LabelResponse) => void = () => {};
    const transport = stubTransport({
      setLabel: vi.fn(
        () =>
          new Promise<LabelResponse>((resolve) => {
            resolveLabel = resolve;
          }),
      ),
    });
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    key('7');
    key('Enter');
    await screen.findByRole('status');
    expect(screen.getByTestId('active-label').textContent).toBe('7'); // optimistic
    await act(async () => {
      resolveLabel({ label_id: 7, since_ms: Date.now() });
    });
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  });

  it('a failed label change shows the message inline and clears pending', async () => {
    const transport = stubTransport({
      setLabel: vi.fn(async () => {
        throw new Error('label refused by backend');
      }),
    });
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    key('3');
    key('Enter');
    await screen.findByTestId('label-error');
    expect(screen.getByTestId('label-error').textContent).toContain('label refused by backend');
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByTestId('active-label').textContent).toBe('0'); // back to the server value
  });

  it('clicking a grid button applies that label', async () => {
    const transport = stubTransport();
    renderWithApp(<LabelPanel />, { snapshot: recordingSnapshot(), transport });
    const seven = screen.getAllByRole('button', { name: '7' })[0];
    fireEvent.click(seven as HTMLElement);
    await waitFor(() => expect(transport.setLabel).toHaveBeenCalledWith(7));
  });
});
