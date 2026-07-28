"use client";
import { useEffect, useRef } from "react";
import type { DeviceInfo } from "@/lib/ws_client";

interface Sample { acc: number[]; gyro: number[]; ts: number }

interface Props {
  devices: DeviceInfo[];
  quorum: { connected: number; roles: string[] };
  liveSamples: Record<string, Sample>;
  isRecording: boolean;
}

const ROLE_COLOR: Record<string, string> = {
  chest:       "bg-blue-500/15 border-blue-500/40",
  waist:       "bg-purple-500/15 border-purple-500/40",
  thigh_left:  "bg-green-500/15 border-green-500/40",
  thigh_right: "bg-teal-500/15 border-teal-500/40",
  ankle_left:  "bg-yellow-500/15 border-yellow-500/40",
  ankle_right: "bg-orange-500/15 border-orange-500/40",
  wrist_left:  "bg-pink-500/15 border-pink-500/40",
  wrist_right: "bg-rose-500/15 border-rose-500/40",
};

function MiniSparkline({ deviceId, samples }: { deviceId: string; samples: Record<string, Sample> }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const bufRef = useRef<number[]>([]);

  useEffect(() => {
    const sample = samples[deviceId];
    if (!sample) return;
    const avm = Math.sqrt(sample.acc.reduce((s, v) => s + v * v, 0));
    bufRef.current.push(avm);
    if (bufRef.current.length > 60) bufRef.current.shift();

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const { width, height } = canvas;
    ctx.clearRect(0, 0, width, height);

    const data = bufRef.current;
    if (data.length < 2) return;
    const min = Math.min(...data);
    const max = Math.max(...data) || 1;

    ctx.strokeStyle = "#22d3ee";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((v - min) / (max - min + 0.001)) * height;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
  }, [samples, deviceId]);

  return <canvas ref={canvasRef} width={120} height={32} className="w-full h-8" />;
}

export default function DevicePanel({ devices, quorum, liveSamples, isRecording }: Props) {
  // Derive from devices directly — quorum counter can lag on reconnect.
  const onlineCount = devices.filter(d => d.is_online).length;
  const allOnline = devices.length > 0 && onlineCount === devices.length;

  return (
    <div>
      {/* Quorum indicator */}
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider">Devices</h3>
        <span className={`text-xs font-bold px-2 py-0.5 rounded ${
          onlineCount === 0
            ? "bg-red-500/15 text-red-400"
            : allOnline
            ? "bg-green-500/15 text-green-400"
            : "bg-yellow-500/15 text-yellow-400"
        }`}>
          {onlineCount}/{devices.length} online
        </span>
      </div>

      {devices.length === 0 && (
        <p className="text-xs text-gray-600 italic">No devices connected</p>
      )}

      <div className="space-y-2">
        {devices.map(d => {
          const colorClass = ROLE_COLOR[d.role] ?? "bg-gray-500/15 border-gray-500/40";
          return (
            <div
              key={d.device_id}
              className={`rounded-lg border px-2 py-2 ${colorClass} ${!d.is_online ? "opacity-40" : ""}`}
            >
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-1.5">
                  <span className={`w-2 h-2 rounded-full ${d.is_online ? "bg-green-400" : "bg-red-400"}`} />
                  <span className="text-xs font-bold text-white">{d.role}</span>
                </div>
                <span className="text-xs text-gray-400 tabular-nums">{d.device_id.slice(0, 8)}</span>
              </div>

              {/* Mini sparkline — acc vector magnitude */}
              <MiniSparkline deviceId={d.device_id} samples={liveSamples} />

              <div className="flex justify-between mt-1 text-xs text-gray-500">
                <span className={d.streaming ? "text-green-400" : "text-gray-500"}>
                  {d.packets?.toLocaleString() ?? 0} pkts · {(d.rate_hz ?? 0).toFixed(0)} Hz
                </span>
                {(d.offline_intervals ?? 0) > 0 && (
                  <span className="text-orange-400">⚠ {d.offline_intervals} gap(s)</span>
                )}
                {isRecording && d.substate === "RECORDING" && (
                  <span className="text-green-400">● live</span>
                )}
              </div>
              {isRecording && d.is_online && d.streaming === false && (
                <div className="mt-1 text-xs text-red-400 font-bold">⚠ no data</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
