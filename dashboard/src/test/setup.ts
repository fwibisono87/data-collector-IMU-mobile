import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom has no 2D canvas: components must cope with getContext() === null
// (CamPreview, uPlot in LiveChart).
HTMLCanvasElement.prototype.getContext = (() =>
  null) as unknown as typeof HTMLCanvasElement.prototype.getContext;

// uPlot probes matchMedia at import time; jsdom does not implement it.
if (typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// This vitest/jsdom combination does not expose localStorage; prefs.ts only
// stores view preferences, so an in-memory stub is a faithful stand-in.
if (typeof globalThis.localStorage === 'undefined') {
  const store = new Map<string, string>();
  const localStorageStub = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, 'localStorage', { value: localStorageStub, writable: true });
  Object.defineProperty(window, 'localStorage', { value: localStorageStub, writable: true });
}

// The live chart observes its host size; jsdom has no ResizeObserver.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  Object.defineProperty(globalThis, 'ResizeObserver', { value: ResizeObserverStub, writable: true });
}

// Done view Copy buttons use the async clipboard.
if (!navigator.clipboard) {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn(async () => undefined) },
    configurable: true,
    writable: true,
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
