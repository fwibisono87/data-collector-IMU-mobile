"use client";
import type { DeviceInfo } from "@/lib/ws_client";
import type { CameraStatus } from "@/components/MultiCameraRecorder";

export type CheckStatus = "pending" | "pass" | "fail";

export interface Check { label: string; status: CheckStatus; detail?: string; }

// Minimum distinct acc readings/sec each ONLINE device must sustain to pass preflight.
// 100 Hz nominal × 90% — see session_manager note_sample (callbacks vs distinct values).
export const SAMPLING_RATE_MIN_HZ = 90;

/** This bundle's identity, baked in at build time by next.config.mjs. */
export const DASHBOARD_BUILD_ID = process.env.NEXT_PUBLIC_BUILD_ID || "unknown";

/**
 * Compare the running dashboard bundle against the backend it is talking to.
 *
 * On 2026-08-11 an operator's browser executed a cached bundle that existed on no disk and
 * matched no commit, against a backend whose payload shape had since moved. It dereferenced a
 * field that no longer arrives and white-screened at the moment a 17-minute session was being
 * saved. Nothing in the UI could have told them. This check can.
 *
 * "unknown" on either side yields `pending`, never `fail`: a build outside a git checkout must
 * not raise a false alarm that scares an operator mid-study.
 */
function buildMatchCheck(backendBuildId: string | null): Check {
  const label = "Dashboard build matches backend";
  const ours = DASHBOARD_BUILD_ID;
  if (!backendBuildId) {
    return { label, status: "pending", detail: "backend not reached yet" };
  }
  if (ours === "unknown" || backendBuildId === "unknown") {
    return { label, status: "pending", detail: "build id unavailable" };
  }
  if (ours === backendBuildId) {
    return { label, status: "pass", detail: ours };
  }
  return { label, status: "fail", detail: `dashboard ${ours} / backend ${backendBuildId}` };
}

function buildChecks(
  isWsConnected: boolean,
  devices: DeviceInfo[],
  subject: string,
  sessionTag: string,
  operator: string,
  camStatus: CameraStatus,
  backendBuildId: string | null = null,
): Check[] {
  const onlineDevices = devices.filter(d => d.is_online);
  return [
    {
      label: "Backend connected",
      status: isWsConnected ? "pass" : "fail",
      detail: isWsConnected ? "OK" : "Not connected",
    },
    buildMatchCheck(backendBuildId),
    {
      label: "At least 1 device online",
      status: onlineDevices.length > 0 ? "pass" : "fail",
      detail: `${onlineDevices.length} online`,
    },
    (() => {
      // true_hz counts DISTINCT accelerometer readings per second, not packets. A phone can
      // stream a confident 100 packets/sec while the OS re-delivers each hardware sample
      // twice — the failure that silently halved two devices on 2026-08-07.
      const label = "Sampling rate healthy";
      // Judge on the smoothed average, not the instantaneous tick: a single 1 s bucket swings
      // 79→100→84 on a healthy device purely from where the tick boundary lands.
      // A device that is connected but not yet streaming reports 0 Hz with 0 packets. That is
      // "hasn't started", not "broken" — counting it as an offender made every fresh
      // connection show a red FAIL at 0 Hz.
      const streaming = onlineDevices.filter(
        d => typeof d.true_hz_avg === "number" && (d.packets ?? 0) > 0,
      );
      const offenders = streaming.filter(
        d => (d.true_hz_avg as number) < SAMPLING_RATE_MIN_HZ,
      );

      // A real offender outranks incomplete reporting: name it immediately rather than
      // hiding a known-bad device behind "awaiting".
      if (offenders.length > 0) {
        return {
          label,
          status: "fail" as CheckStatus,
          detail: offenders
            .map(d => `${d.role} ${(d.true_hz_avg as number).toFixed(0)} Hz`)
            .join(", "),
        };
      }
      // Nothing to evaluate yet, or some device has not produced its first 1 s tick. Both are
      // "pending", never "pass" — a green tick on an unevaluated condition is how a bad
      // session gets started. Note `onlineDevices.every()` on an empty list returns true,
      // which is exactly the trap here.
      if (onlineDevices.length === 0) {
        return { label, status: "pending" as CheckStatus, detail: "No devices online" };
      }
      if (streaming.length < onlineDevices.length) {
        return {
          label,
          status: "pending" as CheckStatus,
          detail: `Awaiting telemetry (${streaming.length}/${onlineDevices.length} streaming)`,
        };
      }
      return { label, status: "pass" as CheckStatus, detail: `all ≥ ${SAMPLING_RATE_MIN_HZ} Hz` };
    })(),
    {
      label: "Subject name",
      status: subject.trim().length > 0 ? "pass" : "fail",
      detail: subject || "Required",
    },
    {
      label: "Session tag",
      status: sessionTag.trim().length > 0 ? "pass" : "fail",
      detail: sessionTag || "Required",
    },
    {
      label: "Operator name",
      status: operator.trim().length > 0 ? "pass" : "fail",
      detail: operator || "Required",
    },
    {
      label: "Cameras ready",
      status: camStatus.ok ? "pass" : "fail",
      detail: camStatus.total === 0 ? "None selected" : `${camStatus.ready}/${camStatus.total} ready`,
    },
  ];
}

interface Props {
  /**
   * Computed by the caller via buildChecks() and passed down, rather than recomputed here.
   *
   * These checks used to be built inside this component while page.tsx separately hand-rolled
   * its own boolean for the START gate — a boolean that omitted "Sampling rate healthy"
   * entirely. The panel could therefore display ✗ NO-GO for a 50 Hz device while START stayed
   * enabled, which is how the rate protection added after 2026-08-07 ended up decorative.
   * One array, one source of truth, shared by the panel and whatever the caller gates on.
   */
  checks: Check[];
  devices: DeviceInfo[];
}

export default function PreflightPanel(props: Props) {
  const checks = props.checks;
  const allPass = checks.every(c => c.status === "pass");

  return (
    <div>
      <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider mb-2">
        Preflight {allPass ? "✓ GO" : "✗ NO-GO"}
      </h3>
      <div className="space-y-1">
        {checks.map(c => (
          <div key={c.label} className="flex items-center gap-2 text-xs">
            <span className={c.status === "pass" ? "text-green-400" : "text-red-400"}>
              {c.status === "pass" ? "✓" : "✗"}
            </span>
            <span className="text-gray-400 flex-1">{c.label}</span>
            <span className={`text-right ${c.status === "pass" ? "text-gray-500" : "text-red-400"}`}>
              {c.detail}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-2">
        {props.devices.length > 0 && (
          <div className="text-xs text-gray-500 space-y-0.5">
            <p className="text-gray-400 font-semibold">Devices</p>
            {props.devices.map(d => (
              <div key={d.device_id} className="flex justify-between">
                <span className={d.is_online ? "text-green-400" : "text-gray-600"}>
                  {d.role}
                </span>
                <span className="text-gray-600">{d.packets.toLocaleString()} pkts</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export { buildChecks };
