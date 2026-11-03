import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { ApiError } from './errors';
import { HttpTransport, type WebSocketLike } from './transport';
import type { ServerMessage } from './types';
import { ReconnectBar } from '../components/ReconnectBar';
import { makeSnapshot, stubTransport } from '../test/helpers';
import { createApp, AppProvider } from '../state/store';

class FakeWebSocket implements WebSocketLike {
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  onopen: ((ev?: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev?: unknown) => void) | null = null;
  onerror: ((ev?: unknown) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.();
  }
  // --- test drivers -------------------------------------------------------
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  receive(msg: unknown): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  receiveRaw(text: string): void {
    this.onmessage?.({ data: text });
  }
  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }
  static last(): FakeWebSocket {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    if (!ws) throw new Error('no FakeWebSocket created');
    return ws;
  }
}

function makeTransport(opts: { fetchImpl?: typeof fetch } = {}): HttpTransport {
  FakeWebSocket.instances = [];
  return new HttpTransport({
    wsUrl: 'ws://test/ws/ui',
    WebSocketImpl: FakeWebSocket as unknown as new (url: string) => WebSocketLike,
    fetchImpl: opts.fetchImpl ?? (vi.fn() as unknown as typeof fetch),
  });
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('HttpTransport reconnect backoff (API.md: 0.5 s doubling to 5 s)', () => {
  it('retries at 500, 1000, 2000, 4000, then caps at 5000', () => {
    const t = makeTransport();
    t.subscribe(() => {});
    expect(FakeWebSocket.instances).toHaveLength(1);

    const attempts = [500, 1000, 2000, 4000, 5000, 5000];
    attempts.forEach((delay, i) => {
      FakeWebSocket.last().drop();
      vi.advanceTimersByTime(delay - 1);
      expect(FakeWebSocket.instances).toHaveLength(i + 1); // not yet
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.instances).toHaveLength(i + 2); // retried
    });
    t.dispose();
  });

  it('resets the backoff after a successful open', () => {
    const t = makeTransport();
    t.subscribe(() => {});
    FakeWebSocket.last().drop();
    vi.advanceTimersByTime(500);
    FakeWebSocket.last().drop();
    vi.advanceTimersByTime(1000);
    FakeWebSocket.last().open(); // success — backoff back to 500
    FakeWebSocket.last().drop();
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(4);
    t.dispose();
  });

  it('reports connectivity, and after reconnect takes the fresh snapshot without reconciling', () => {
    const statuses: boolean[] = [];
    const messages: ServerMessage[] = [];
    const t = makeTransport();
    t.subscribe((m) => messages.push(m), (c) => statuses.push(c));

    const ws1 = FakeWebSocket.last();
    ws1.open();
    expect(statuses).toEqual([true]);
    ws1.receive({ type: 'snapshot', data: makeSnapshot({ state: 'RECORDING' }) });

    ws1.drop();
    expect(statuses).toEqual([true, false]);

    vi.advanceTimersByTime(500);
    const ws2 = FakeWebSocket.last();
    ws2.open();
    expect(statuses).toEqual([true, false, true]);
    ws2.receive({ type: 'snapshot', data: makeSnapshot({ state: 'IDLE' }) });

    // The second snapshot simply REPLACES the first; nothing is merged.
    expect(messages).toHaveLength(2);
    expect((messages[1] as { data: { state: string } }).data.state).toBe('IDLE');
    t.dispose();
  });

  it('ignores garbage frames and forwards unknown types for the store to drop', () => {
    const messages: ServerMessage[] = [];
    const t = makeTransport();
    t.subscribe((m) => messages.push(m));
    const ws = FakeWebSocket.last();
    ws.open();
    ws.receiveRaw('this is not json{{{');
    expect(messages).toHaveLength(0);
    ws.receive({ type: 'totally.new', data: {} });
    expect(messages).toHaveLength(1); // forwarded; store.test proves the store ignores it
    t.dispose();
  });

  it('dispose() closes the socket without scheduling a reconnect', () => {
    const t = makeTransport();
    t.subscribe(() => {});
    const ws = FakeWebSocket.last();
    t.dispose();
    expect(ws.closed).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe('HttpTransport WS commands (calibration.watch / unwatch)', () => {
  it('sends a req_id and resolves on the matching ack', async () => {
    const messages: ServerMessage[] = [];
    const t = makeTransport();
    t.subscribe((m) => messages.push(m));
    const ws = FakeWebSocket.last();
    ws.open();

    const p = t.watchCalibration('active');
    expect(ws.sent).toHaveLength(1);
    const msg = JSON.parse(ws.sent[0] ?? '{}') as Record<string, unknown>;
    expect(msg['type']).toBe('calibration.watch');
    expect(msg['ref_id']).toBe('active');
    expect(typeof msg['req_id']).toBe('string');

    ws.receive({ type: 'ack', req_id: msg['req_id'], ok: true });
    await expect(p).resolves.toBeUndefined();
    // acks are consumed by the transport, never forwarded to the store
    expect(messages).toHaveLength(0);
    t.dispose();
  });

  it('rejects with an ApiError when the ack carries an error', async () => {
    const t = makeTransport();
    t.subscribe(() => {});
    const ws = FakeWebSocket.last();
    ws.open();
    const p = t.unwatchCalibration();
    const msg = JSON.parse(ws.sent[0] ?? '{}') as Record<string, unknown>;
    ws.receive({
      type: 'ack',
      req_id: msg['req_id'],
      ok: false,
      error: { code: 'bad_state', message: 'not watching' },
    });
    await expect(p).rejects.toMatchObject({ code: 'bad_state', message: 'not watching' });
    t.dispose();
  });

  it('rejects pending commands when the socket drops', async () => {
    const t = makeTransport();
    t.subscribe(() => {});
    const ws = FakeWebSocket.last();
    ws.open();
    const p = t.watchCalibration('active');
    ws.drop();
    await expect(p).rejects.toBeInstanceOf(ApiError);
    t.dispose();
  });
});

describe('HttpTransport REST errors', () => {
  it('turns a 409 blocked body into an ApiError with blockers', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(409, {
        error: {
          code: 'blocked',
          message: 'START refused',
          details: { blockers: [{ code: 'no_phones', message: 'No phone online' }] },
        },
      }),
    ) as unknown as typeof fetch;
    const t = makeTransport({ fetchImpl });
    const err = await t.startSession().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(409);
    expect(apiErr.code).toBe('blocked');
    expect(apiErr.blockers).toHaveLength(1);
    t.dispose();
  });

  it('turns a network failure into ApiError status 0', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error('fetch failed');
    }) as unknown as typeof fetch;
    const t = makeTransport({ fetchImpl });
    await expect(t.getState()).rejects.toMatchObject({ status: 0, code: 'network' });
    t.dispose();
  });
});

describe('ReconnectBar', () => {
  it('is hidden while connected and shows while down', () => {
    const transport = stubTransport();
    const app = createApp(transport);
    app.ui.setState({ connected: true, snapshot: makeSnapshot() });
    const { rerender } = render(
      <AppProvider value={app}>
        <ReconnectBar />
      </AppProvider>,
    );
    expect(screen.queryByRole('status')).toBeNull();

    app.ui.setState({ connected: false, snapshot: makeSnapshot() });
    rerender(
      <AppProvider value={app}>
        <ReconnectBar />
      </AppProvider>,
    );
    expect(screen.getByRole('status').textContent).toContain('Reconnecting');
  });

  it('follows the transport status callback end to end', () => {
    const transport = stubTransport();
    const app = createApp(transport);
    render(
      <AppProvider value={app}>
        <ReconnectBar />
      </AppProvider>,
    );
    act(() => app.start()); // subscribe wires onStatus through the stub
    // Not connected yet → the bar explains it.
    expect(screen.getByRole('status').textContent).toContain('Reconnecting');
    act(() => transport.setConnected(true));
    expect(screen.queryByRole('status')).toBeNull();
    act(() => transport.setConnected(false));
    expect(screen.getByRole('status').textContent).toContain('Reconnecting');
    app.dispose();
  });
});
