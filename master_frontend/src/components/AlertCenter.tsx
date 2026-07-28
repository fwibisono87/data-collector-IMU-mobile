"use client";
import { useEffect, useRef, useState } from "react";
import type { DeviceInfo, SessionState } from "@/lib/ws_client";
import { playAlert, startRepeating, isMuted, setMuted, armAudio } from "@/lib/alert_sound";

export interface ConnEvent {
  ts: number;
  kind: "lost" | "back" | "ws_lost" | "ws_back" | "no_data";
  who: string;
  gapMs?: number;
}

interface Props { devices: DeviceInfo[]; state: SessionState; isWsConnected: boolean }

export default function AlertCenter({ devices, state, isWsConnected }: Props) {
  const [events, setEvents] = useState<ConnEvent[]>([]);
  const [muted, setMutedState] = useState(false);
  const prevOnline = useRef<Map<string, boolean>>(new Map());
  const prevStreaming = useRef<Map<string, boolean>>(new Map());
  const lostAt = useRef<Map<string, number>>(new Map());
  const stopRepeat = useRef<null | (() => void)>(null);
  const prevWs = useRef(true);

  useEffect(() => setMutedState(isMuted()), []);

  // Device online/offline transitions, derived from the authoritative STATE_UPDATE list.
  // Also flags a device that is online (control channel alive) but sending no telemetry —
  // the only signal for a green card silently producing nothing (plan D15).
  useEffect(() => {
    const now = Date.now();
    const nextOnline = new Map<string, boolean>();
    const added: ConnEvent[] = [];

    for (const d of devices) {
      nextOnline.set(d.device_id, d.is_online);
      const was = prevOnline.current.get(d.device_id);
      if (was !== undefined) {
        if (was && !d.is_online) {
          lostAt.current.set(d.device_id, now);
          added.push({ ts: now, kind: "lost", who: d.role });
        } else if (!was && d.is_online) {
          const t = lostAt.current.get(d.device_id);
          added.push({ ts: now, kind: "back", who: d.role, gapMs: t ? now - t : undefined });
          lostAt.current.delete(d.device_id);
        }
      }

      if (state === "RECORDING" && d.is_online) {
        const wasStreaming = prevStreaming.current.get(d.device_id);
        const isStreaming = d.streaming ?? true;   // undefined (older backend) => don't alert
        if (wasStreaming === true && !isStreaming) {
          added.push({ ts: now, kind: "no_data", who: d.role });
        }
        prevStreaming.current.set(d.device_id, isStreaming);
      }
    }

    // A device REMOVED from the list while recording is also a loss (backend pruned it).
    prevOnline.current.forEach((was, id) => {
      if (was && !nextOnline.has(id) && state === "RECORDING") {
        added.push({ ts: now, kind: "lost", who: id.slice(0, 8) });
      }
    });
    prevOnline.current = nextOnline;

    if (added.length) setEvents(e => [...added, ...e].slice(0, 200));
    if (added.some(a => a.kind === "lost" || a.kind === "no_data") && state === "RECORDING") {
      playAlert("device_lost");
    }
    if (added.some(a => a.kind === "back")) playAlert("device_back");
  }, [devices, state]);

  // Dashboard's own backend link.
  useEffect(() => {
    if (prevWs.current && !isWsConnected) {
      const ev: ConnEvent = { ts: Date.now(), kind: "ws_lost", who: "backend" };
      setEvents(e => [ev, ...e].slice(0, 200));
      playAlert("ws_lost");
    } else if (!prevWs.current && isWsConnected) {
      const ev: ConnEvent = { ts: Date.now(), kind: "ws_back", who: "backend" };
      setEvents(e => [ev, ...e].slice(0, 200));
      playAlert("ws_back");
    }
    prevWs.current = isWsConnected;
  }, [isWsConnected]);

  // Keep alarming while a device is down OR sending no data DURING a recording — one
  // beep is missable across a room.
  const offline = devices.filter(d => !d.is_online);
  const noData = devices.filter(d => state === "RECORDING" && d.is_online && d.streaming === false);
  const alarming = state === "RECORDING" && (offline.length > 0 || noData.length > 0 || !isWsConnected);
  useEffect(() => {
    if (alarming && !stopRepeat.current) stopRepeat.current = startRepeating("device_lost", 4000);
    if (!alarming && stopRepeat.current) { stopRepeat.current(); stopRepeat.current = null; }
    return () => { stopRepeat.current?.(); stopRepeat.current = null; };
  }, [alarming]);

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    setMutedState(next);
    armAudio();
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider">Connectivity</h3>
        <div className="flex items-center gap-2">
          <button
            onClick={() => playAlert("test", true)}
            className="text-[10px] text-gray-500 hover:text-gray-300 underline"
          >
            Test
          </button>
          <button
            onClick={toggleMute}
            className="text-sm leading-none"
            title={muted ? "Unmute alerts" : "Mute alerts"}
          >
            {muted ? "🔕" : "🔔"}
          </button>
        </div>
      </div>

      {events.length === 0 ? (
        <p className="text-xs text-gray-600 italic">No connectivity events</p>
      ) : (
        <div className="max-h-40 overflow-y-auto space-y-0.5 pr-1">
          {events.map((e, i) => (
            <div
              key={`${e.ts}-${i}`}
              className={`text-[11px] tabular-nums ${
                e.kind === "lost" || e.kind === "ws_lost"
                  ? "text-red-400"
                  : e.kind === "no_data"
                  ? "text-orange-400"
                  : "text-green-400"
              }`}
            >
              {new Date(e.ts).toLocaleTimeString()} · {e.who}{" "}
              {e.kind === "lost" && "OFFLINE"}
              {e.kind === "back" && `back ONLINE${e.gapMs ? ` after ${Math.round(e.gapMs / 1000)}s` : ""}`}
              {e.kind === "no_data" && "ONLINE but sending no data"}
              {e.kind === "ws_lost" && "backend link lost"}
              {e.kind === "ws_back" && "backend link restored"}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
