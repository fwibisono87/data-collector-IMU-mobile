import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { PanelBoundary } from './components/PanelBoundary';
import { ReconnectBar } from './components/ReconnectBar';
import { TopBar } from './components/TopBar';
import { DevPanel } from './dev/DevPanel';
import { CalibrationView } from './views/CalibrationView';
import { DoneView } from './views/DoneView';
import { Home } from './views/Home';
import { SessionsView } from './views/SessionsView';

/**
 * App shell: fixed top bar + reconnect bar, the rest is the router. The page
 * itself never scrolls (1366×768 laptop target); views manage their own
 * internal scrolling. Unknown paths go home — home follows backend state.
 */
export function AppShell() {
  return (
    <div className="flex h-screen flex-col bg-zinc-950 text-zinc-100">
      <PanelBoundary name="Top bar">
        <TopBar />
      </PanelBoundary>
      <PanelBoundary name="Connection">
        <ReconnectBar />
      </PanelBoundary>
      <main className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/sessions" element={<SessionsView />} />
          <Route path="/sessions/:sid" element={<SessionRoute />} />
          <Route
            path="/calibration"
            element={
              <PanelBoundary name="Calibration" className="h-full min-h-0">
                <CalibrationView />
              </PanelBoundary>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <DevPanel />
    </div>
  );
}

function SessionRoute() {
  const { sid } = useParams<{ sid: string }>();
  if (!sid) return <Navigate to="/sessions" replace />;
  return (
    <PanelBoundary name="Session" className="h-full min-h-0">
      <DoneView sid={decodeURIComponent(sid)} />
    </PanelBoundary>
  );
}
