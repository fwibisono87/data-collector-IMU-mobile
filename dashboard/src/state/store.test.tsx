import { act, render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { LiveMsg, ServerMessage, SnapshotMsg, DriftMsg, FinalizeMsg } from '../api/types';
import { stubTransport, makeSnapshot, makeSession } from '../test/helpers';
import { AppProvider, createApp, useSnapshot } from './store';

function liveMsg(t: number): LiveMsg {
  return {
    type: 'live',
    data: {
      t_ms: t,
      phones: { 'dev-chest': { acc: [0, 0, 1], gyro: [0, 0, 0], ts_ms: t } },
    },
  };
}

describe('ui store', () => {
  it('replaces the snapshot wholesale — never patches', () => {
    const app = createApp(stubTransport());
    const s1 = makeSnapshot({ state: 'IDLE' });
    const s2 = makeSnapshot({ state: 'RECORDING', session: makeSession() });
    app.dispatch({ type: 'snapshot', data: s1 } as SnapshotMsg);
    expect(app.ui.getState().snapshot?.state).toBe('IDLE');
    app.dispatch({ type: 'snapshot', data: s2 } as SnapshotMsg);
    // Wholesale: the store holds exactly what the server sent, fields from s1
    // that s2 does not carry must not leak through.
    expect(app.ui.getState().snapshot).toBe(s2);
    expect(app.ui.getState().snapshot?.state).toBe('RECORDING');
  });

  it('ignores unknown message types (forward compatibility)', () => {
    const app = createApp(stubTransport());
    app.dispatch({ type: 'snapshot', data: makeSnapshot() } as SnapshotMsg);
    const before = app.ui.getState();
    expect(() =>
      app.dispatch({ type: 'something.from.v4', data: { x: 1 } } as unknown as ServerMessage),
    ).not.toThrow();
    expect(app.ui.getState()).toBe(before);
    expect(app.live.getState()).toBeNull();
  });

  it('ignores snapshot messages with an unusable payload', () => {
    const app = createApp(stubTransport());
    app.dispatch({ type: 'snapshot', data: { nonsense: true } } as unknown as SnapshotMsg);
    expect(app.ui.getState().snapshot).toBeNull();
  });

  it('merges finalize progress into the current snapshot', () => {
    const app = createApp(stubTransport());
    const snap = makeSnapshot({ state: 'FINALIZING', session: makeSession() });
    app.dispatch({ type: 'snapshot', data: snap } as SnapshotMsg);
    const progress = {
      session_id: 's-test-1',
      steps: [
        { name: 'validate' as const, status: 'running' as const, started_at_ms: 1, ended_at_ms: null, detail: null },
      ],
    };
    app.dispatch({ type: 'finalize', data: progress } as FinalizeMsg);
    const next = app.ui.getState().snapshot;
    expect(next?.finalize?.session_id).toBe('s-test-1');
    expect(next?.finalize?.steps[0]?.status).toBe('running');
    // The rest of the snapshot is untouched.
    expect(next?.state).toBe('FINALIZING');
  });

  it('routes live samples to the isolated live store only', () => {
    const app = createApp(stubTransport());
    app.dispatch({ type: 'snapshot', data: makeSnapshot() } as SnapshotMsg);
    const uiBefore = app.ui.getState();
    app.dispatch(liveMsg(42));
    expect(app.live.getState()?.t_ms).toBe(42);
    expect(app.ui.getState()).toBe(uiBefore); // ui store untouched
  });

  it('routes calibration drift to the isolated drift store only', () => {
    const app = createApp(stubTransport());
    app.dispatch({ type: 'snapshot', data: makeSnapshot() } as SnapshotMsg);
    const uiBefore = app.ui.getState();
    app.dispatch({
      type: 'calibration.drift',
      data: { ref_id: 'r1', at_ms: 1, cameras: [] },
    } as DriftMsg);
    expect(app.drift.getState()?.ref_id).toBe('r1');
    expect(app.ui.getState()).toBe(uiBefore);
  });
});

describe('render isolation (10 Hz live data)', () => {
  it('100 live messages re-render the page shell 0 times', () => {
    const app = createApp(stubTransport());
    app.ui.setState({ connected: true, snapshot: makeSnapshot({ state: 'RECORDING', session: makeSession() }) });

    let renders = 0;
    function Shell() {
      renders += 1;
      const state = useSnapshot((s) => s.snapshot?.state ?? null);
      return <div data-testid="shell">{state}</div>;
    }
    render(
      <AppProvider value={app}>
        <Shell />
      </AppProvider>,
    );
    expect(renders).toBe(1);

    act(() => {
      for (let i = 0; i < 100; i += 1) app.dispatch(liveMsg(i));
    });
    expect(renders).toBe(1); // live data never touches React

    // A real snapshot update still re-renders.
    act(() => {
      app.dispatch({ type: 'snapshot', data: makeSnapshot({ state: 'FINALIZING' }) } as SnapshotMsg);
    });
    expect(renders).toBe(2);
  });
});
