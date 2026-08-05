"use client";
import { useEffect, useState } from "react";

interface Props {
  activeLabel: number;
  onLabel: (id: number) => void;
  disabled: boolean;
}

// Label 0 = baseline (name "0"). Labels 1–50 per MIGRATION_PLAN.md open decision #3.
const LABELS = Array.from({ length: 51 }, (_, i) => i);   // [0, 1, ..., 50]

function _fmt(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function LabelingPanel({ activeLabel, onLabel, disabled }: Props) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    setElapsed(0);
    const t = setInterval(() => setElapsed(e => e + 1), 1000);
    return () => clearInterval(t);
  }, [activeLabel]);

  return (
    <div>
      <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider mb-2">
        Labels{" "}
        <span className="text-accent normal-case">— active: {activeLabel}</span>
        <span className="ml-2 text-cyan-300 normal-case font-semibold tabular-nums">
          {_fmt(elapsed)}
        </span>
      </h3>
      <div className="grid grid-cols-10 gap-1">
        {LABELS.map(id => (
          <button
            key={id}
            onClick={() => onLabel(id)}
            disabled={disabled}
            className={`
              rounded py-1 text-xs font-bold tabular-nums transition-all
              ${activeLabel === id
                ? "bg-accent text-[#04141a] shadow-[0_0_12px_rgba(34,211,238,0.45)]"
                : id === 0
                  ? "glass-card text-gray-500 hover:border-accent/50 hover:text-white"
                  : "glass-card text-gray-400 hover:border-accent/50 hover:text-white"}
              disabled:opacity-30 disabled:cursor-not-allowed
            `}
          >
            {id}
          </button>
        ))}
      </div>
    </div>
  );
}
