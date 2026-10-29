import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useSyncExternalStore,
} from 'react';
import type {
  CompareResult,
  DriftMsg,
  FinalizeMsg,
  LiveData,
  LiveMsg,
  ServerMessage,
  Snapshot,
  SnapshotMsg,
} from '../api/types';
import type { Transport } from '../api/transport';

/**
 * Tiny external stores read through useSyncExternalStore.
 *
 * Rules (L6 spec):
 * - `ui` holds {connected, snapshot}: the ONLY state the page renders from.
 *   Snapshots are replaced wholesale — no patch logic, no reconciliation.
 * - `live` (10 Hz IMU samples) is a SEPARATE store only the chart subscribes
 *   to, so live data never re-renders the page (proven by a render-count test).
 * - `drift` (2 Hz calibration verdicts) is likewise isolated to the badges.
 * - Selectors passed to useSnapshot must return stable references (primitives
 *   or sub-objects of the state), never freshly-built objects.
 */

export interface UiState {
  connected: boolean;
  snapshot: Snapshot | null;
}

export class Store<T> {
  private state: T;
  private readonly listeners = new Set<() => void>();

  constructor(initial: T) {
    this.state = initial;
  }

  getState = (): T => this.state;

  setState(next: T): void {
    if (Object.is(next, this.state)) return;
    this.state = next;
    for (const l of [...this.listeners]) l();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
}

export interface App {
  transport: Transport;
  ui: Store<UiState>;
  live: Store<LiveData | null>;
  drift: Store<CompareResult | null>;
  /** Feed one WS message in (exported for tests and the mock dev panel). */
  dispatch(msg: ServerMessage): void;
  setConnected(connected: boolean): void;
  /** Initial REST fetch + WS subscription. */
  start(): void;
  dispose(): void;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}

export function createApp(transport: Transport): App {
  const ui = new Store<UiState>({ connected: false, snapshot: null });
  const live = new Store<LiveData | null>(null);
  const drift = new Store<CompareResult | null>(null);

  function setConnected(connected: boolean): void {
    const cur = ui.getState();
    if (cur.connected === connected) return;
    ui.setState({ ...cur, connected });
  }

  function dispatch(msg: ServerMessage): void {
    // Unknown message types are ignored (forward compatibility, API.md).
    if (!isRecord(msg)) return;
    switch ((msg as { type?: unknown }).type) {
      case 'snapshot': {
        const data = (msg as SnapshotMsg).data;
        if (isRecord(data) && typeof data['state'] === 'string') {
          ui.setState({ ...ui.getState(), snapshot: data as unknown as Snapshot });
        }
        break;
      }
      case 'live': {
        const data = (msg as LiveMsg).data;
        if (isRecord(data)) live.setState(data as unknown as LiveData);
        break;
      }
      case 'finalize': {
        // Also reflected in the next snapshot; merged here so the Done view
        // updates immediately when a step changes.
        const data = (msg as FinalizeMsg).data;
        const cur = ui.getState();
        if (cur.snapshot && isRecord(data)) {
          ui.setState({
            ...cur,
            snapshot: { ...cur.snapshot, finalize: data as unknown as Snapshot['finalize'] },
          });
        }
        break;
      }
      case 'calibration.drift': {
        const data = (msg as DriftMsg).data;
        if (isRecord(data)) drift.setState(data as unknown as CompareResult);
        break;
      }
      default:
        break;
    }
  }

  let unsubscribe: (() => void) | null = null;

  function start(): void {
    unsubscribe = transport.subscribe(dispatch, setConnected);
    // Render as soon as the REST snapshot lands, even before the WS connects.
    transport
      .getState()
      .then((snap) => {
        const cur = ui.getState();
        if (!cur.snapshot && isRecord(snap)) {
          ui.setState({ ...cur, snapshot: snap });
        }
      })
      .catch(() => {
        // Backend unreachable: the reconnecting bar explains it; the WS
        // snapshot fills the store when it comes back.
      });
  }

  function dispose(): void {
    unsubscribe?.();
    unsubscribe = null;
    transport.dispose();
  }

  return { transport, ui, live, drift, dispatch, setConnected, start, dispose };
}

const AppContext = createContext<App | null>(null);

export const AppProvider = AppContext.Provider;

export function useApp(): App {
  const app = useContext(AppContext);
  if (!app) throw new Error('useApp() must be used inside <AppProvider>');
  return app;
}

export function useTransport(): Transport {
  return useApp().transport;
}

/** Subscribe to any Store with a selector; re-renders only when the selected value changes. */
export function useStoreValue<S, T>(store: Store<S>, sel: (s: S) => T): T {
  const selRef = useRef(sel);
  selRef.current = sel;
  const cache = useRef<{ has: boolean; src?: S; out?: T }>({ has: false });
  const get = useCallback(() => {
    const s = store.getState();
    const c = cache.current;
    if (!c.has || c.src !== s) {
      const out = selRef.current(s);
      cache.current = { has: true, src: s, out };
      return out;
    }
    return c.out as T;
  }, [store]);
  return useSyncExternalStore(store.subscribe, get, get);
}

/** Read the UI state ({connected, snapshot}) with a selector. */
export function useSnapshot<T>(sel: (s: UiState) => T): T {
  const { ui } = useApp();
  return useStoreValue(ui, sel);
}
