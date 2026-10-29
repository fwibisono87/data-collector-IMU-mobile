import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  name: string;
  children: ReactNode;
  className?: string;
}

interface State {
  failed: boolean;
}

/**
 * Every panel is wrapped in one of these: a render error inside a panel shows
 * "This panel failed — Reload panel" while the rest of the page keeps working.
 * The old dashboard died as a whole page at exactly the worst moments
 * (SPEC §1.1); a dead panel must never take the operator's session with it.
 */
export class PanelBoundary extends Component<Props, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep a trace in the console for the report; the UI stays calm.
    console.error(`[panel:${this.props.name}]`, error, info.componentStack);
  }

  override render(): ReactNode {
    const { name, className, children } = this.props;
    if (this.state.failed) {
      return (
        <div
          className={`rounded-lg border border-red-500/50 bg-red-950/40 p-4 text-sm text-red-200 ${className ?? ''}`}
          role="alert"
        >
          <p className="font-semibold">This panel failed — Reload panel</p>
          <p className="mt-1 text-xs text-red-300/70">Panel: {name}. The rest of the page keeps working.</p>
          <button
            type="button"
            className="mt-2 rounded border border-red-400/60 px-3 py-1 font-bold hover:bg-red-500/20"
            onClick={() => this.setState({ failed: false })}
          >
            Reload panel
          </button>
        </div>
      );
    }
    return (
      <div className={className} data-panel={name}>
        {children}
      </div>
    );
  }
}
