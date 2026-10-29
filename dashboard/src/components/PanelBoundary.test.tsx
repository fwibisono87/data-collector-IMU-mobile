import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PanelBoundary } from './PanelBoundary';

function Boom({ message }: { message: string }): JSX.Element {
  throw new Error(message);
}

describe('PanelBoundary (SPEC §1.1: a dead panel never takes the page down)', () => {
  it('a throwing panel leaves the other panels visible', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <div>
        <PanelBoundary name="Broken">
          <Boom message="kaput" />
        </PanelBoundary>
        <PanelBoundary name="Healthy">
          <div data-testid="healthy-panel">still working</div>
        </PanelBoundary>
      </div>,
    );
    expect(screen.getByRole('alert').textContent).toContain('This panel failed — Reload panel');
    expect(screen.getByRole('alert').textContent).toContain('Broken');
    expect(screen.getByTestId('healthy-panel').textContent).toBe('still working');
    expect(spy).toHaveBeenCalled(); // the trace stays in the console for the report
    spy.mockRestore();
  });

  it('"Reload panel" retries rendering the panel', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    let shouldThrow = true;
    function Flaky(): JSX.Element {
      if (shouldThrow) throw new Error('flaky');
      return <div data-testid="recovered">back</div>;
    }
    render(
      <PanelBoundary name="Flaky">
        <Flaky />
      </PanelBoundary>,
    );
    expect(screen.getByRole('alert').textContent).toContain('This panel failed');
    shouldThrow = false;
    fireEvent.click(screen.getByRole('button', { name: 'Reload panel' }));
    expect(screen.getByTestId('recovered').textContent).toBe('back');
    spy.mockRestore();
  });
});
