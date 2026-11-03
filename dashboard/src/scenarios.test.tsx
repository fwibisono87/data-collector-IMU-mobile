import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { AppShell } from './App';
import { MockTransport, SCENARIOS } from './api/mock';
import { AppProvider, createApp, type App } from './state/store';

/**
 * Mounts the FULL app against every mock scenario and fails on any
 * console.error / console.warn — the automated stand-in for the manual
 * `npm run dev:mock` browser pass (L6 spec: "fix every console error or
 * warning"). Real Chrome checks the rendered pixels; this keeps the console
 * clean in CI too.
 */

let app: App | null = null;
let errorSpy: ReturnType<typeof vi.spyOn>;
let warnSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.useFakeTimers();
  errorSpy = vi.spyOn(console, 'error');
  warnSpy = vi.spyOn(console, 'warn');
});

afterEach(() => {
  if (app) {
    act(() => app?.dispose());
    app = null;
  }
  vi.useRealTimers();
  errorSpy.mockRestore();
  warnSpy.mockRestore();
});

async function mountScenario(id: (typeof SCENARIOS)[number]['id'], route: string) {
  const transport = new MockTransport(id, { latencyMs: 0 });
  app = createApp(transport);
  act(() => app?.start());
  render(
    <AppProvider value={app}>
      <MemoryRouter
        initialEntries={[route]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <AppShell />
      </MemoryRouter>
    </AppProvider>,
  );
  // Let the connect snapshot, telemetry ticks and any scripted events land.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_600);
  });
}

describe.each(SCENARIOS)('scenario "$id" mounts the full app cleanly', ({ id }) => {
  it('no console errors or warnings at /', async () => {
    await mountScenario(id, '/');
    expect(app?.ui.getState().snapshot).not.toBeNull(); // the snapshot arrived
    expect(errorSpy).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();
  });
});

it('scenario "calibration" also mounts /calibration cleanly (watch → drift badges)', async () => {
  await mountScenario('calibration', '/calibration');
  expect(app?.ui.getState().snapshot).not.toBeNull();
  expect(app?.drift.getState()).not.toBeNull(); // drift messages reached the badge store
  expect(errorSpy).not.toHaveBeenCalled();
  expect(warnSpy).not.toHaveBeenCalled();
});
