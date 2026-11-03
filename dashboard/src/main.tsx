import { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AppShell } from './App';
import { MockTransport, SCENARIOS, type ScenarioId } from './api/mock';
import { HttpTransport } from './api/transport';
import { MockProvider } from './dev/mockContext';
import './index.css';
import { AppProvider, createApp } from './state/store';

function initialScenario(): ScenarioId {
  const q = new URLSearchParams(window.location.search).get('scenario');
  return SCENARIOS.some((s) => s.id === q) ? (q as ScenarioId) : 'idle';
}

/**
 * Root: builds ONE app (transport + stores) and provides it to the tree.
 *
 * No StrictMode on purpose: the transport is disposable (subscribe/dispose
 * in an effect) and StrictMode's double-mount would tear down the live
 * socket on every load for no benefit.
 *
 * Under `npm run dev:mock` the transport is the in-memory MockTransport and
 * the `?scenario=` query param (kept in sync by the DevPanel) picks the
 * scripted backend; in production it is the HttpTransport talking to FastAPI.
 */
function Root() {
  const isMock = import.meta.env.VITE_MOCK === '1';
  const [scenario, setScenarioState] = useState<ScenarioId>(initialScenario);

  const app = useMemo(
    () => createApp(isMock ? new MockTransport(scenario) : new HttpTransport()),
    [isMock, scenario],
  );

  useEffect(() => {
    app.start();
    return () => app.dispose();
  }, [app]);

  const setScenario = useMemo(
    () => (id: ScenarioId) => {
      const url = new URL(window.location.href);
      url.searchParams.set('scenario', id);
      window.history.replaceState(null, '', url.toString());
      setScenarioState(id);
    },
    [],
  );

  return (
    <AppProvider value={app}>
      <MockProvider value={isMock ? { scenario, setScenario } : null}>
        <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <AppShell />
        </BrowserRouter>
      </MockProvider>
    </AppProvider>
  );
}

const container = document.getElementById('root');
if (!container) throw new Error('#root container missing');
createRoot(container).render(<Root />);
