"use client";
import React from "react";

/**
 * Render-error containment for end-of-session UI.
 *
 * The dashboard renders server-supplied JSON (integrity report, export manifest) that is
 * typed only by interfaces and, in places, passed through `as unknown as` casts. A
 * backend/frontend version skew that drops or renames a list therefore surfaces as a
 * runtime TypeError *during render* — and an uncaught one unmounts the whole React tree.
 *
 * That failure mode is at its worst precisely where it happened in the field: after a
 * 40-minute recording, at the moment the operator clicks save. The recording itself is
 * already durable on the SSD and on every phone by then, so a summary view failing to
 * draw must never look like — or turn into — losing the session.
 *
 * Error boundaries catch render, lifecycle and (React 18+) passive-effect errors. They do
 * not catch event-handler or async errors, so this is containment, not a substitute for
 * guarding the data access itself.
 */
interface Props {
  children: React.ReactNode;
  /** Short label for what failed, e.g. "integrity report". */
  what: string;
  recoveryHref?: string;
}

interface State {
  error: Error | null;
}

export default class SafeBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Keep the detail in the console for diagnosis; the UI stays usable.
    console.error(`[SafeBoundary] ${this.props.what} failed to render:`, error, info);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm">
        <div className="font-bold text-amber-300 mb-1">
          Could not display the {this.props.what}
        </div>
        <p className="text-[11px] text-amber-200/90">
          This is a display problem only. Use the backend bundle link below, copy the session
          folder from the SSD, or use the Recovery screen to pull the phone copies. The bundle
          remains available even when this dashboard cannot render the summary.
        </p>
        <p className="text-[10px] text-gray-500 mt-1 font-mono break-all">{String(error?.message ?? error)}</p>
        {this.props.recoveryHref && (
          <a
            href={this.props.recoveryHref}
            download
            className="inline-block mt-2 mr-3 text-[11px] text-cyan-300 underline"
          >
            Download backend data bundle
          </a>
        )}
        <button
          onClick={() => this.setState({ error: null })}
          className="mt-2 text-[11px] underline text-cyan-400 hover:text-cyan-300"
        >
          Try again
        </button>
      </div>
    );
  }
}
