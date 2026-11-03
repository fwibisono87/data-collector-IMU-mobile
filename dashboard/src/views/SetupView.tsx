import { PanelBoundary } from '../components/PanelBoundary';
import { CamerasPanel } from './setup/CamerasPanel';
import { PhonesPanel } from './setup/PhonesPanel';
import { ReadinessPanel } from './setup/ReadinessPanel';
import { SessionForm } from './setup/SessionForm';

/**
 * Setup (SPEC §5.1): three columns + readiness, laid out for a 1366×768
 * laptop with NO page scroll (panels scroll internally). Every panel has its
 * own error boundary: one broken panel never takes the page down.
 */
export function SetupView() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-2" data-testid="setup-view">
      <div className="grid min-h-0 flex-1 grid-cols-[280px_minmax(0,1fr)_minmax(0,1.4fr)] gap-2">
        <PanelBoundary name="Session form" className="min-h-0">
          <SessionForm />
        </PanelBoundary>
        <PanelBoundary name="Phones" className="min-h-0">
          <PhonesPanel />
        </PanelBoundary>
        <PanelBoundary name="Cameras" className="min-h-0">
          <CamerasPanel />
        </PanelBoundary>
      </div>
      <PanelBoundary name="Readiness">
        <ReadinessPanel />
      </PanelBoundary>
    </div>
  );
}
