import { useSnapshot } from '../state/store';

/**
 * Top bar shown while the WebSocket is down (API.md client rules). When the
 * socket returns, the fresh snapshot simply replaces the old one — the UI
 * never reconciles anything itself.
 */
export function ReconnectBar() {
  const connected = useSnapshot((s) => s.connected);
  if (connected) return null;
  return (
    <div
      role="status"
      className="shrink-0 bg-amber-400 py-1 text-center text-sm font-black text-black"
    >
      Reconnecting… — waiting for the backend
    </div>
  );
}
