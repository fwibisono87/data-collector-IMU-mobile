"""
Standalone soak-test monitor for the physical multi-phone/multi-camera endurance test.

This harness is instrumentation only: it observes the master backend and fault-injects
named phones while the operator runs the dashboard manually. The dashboard owns the
browser MediaRecorder cameras and IndexedDB camera chunks, and the backend exposes
GET /health and GET /session. The monitor:

  * waits for the backend to enter RECORDING (unless --no-wait-for-recording),
  * polls /health and /session every poll_sec and appends one JSON object per
    observation to JSONL evidence files,
  * discovers and periodically snapshots `adb devices`,
  * executes scheduled, explicitly scoped ADB fault actions against named phone
    serials relative to the observed recording start.

It NEVER sends START_SESSION/STOP_SESSION, NEVER touches browser MediaRecorder
cameras, and NEVER runs destructive ADB commands (no uninstall, no pm clear, no
reboot, no repository mutation).

Plan file schema (JSON):
    duration_sec         number > 0   total planned recording observation time
    poll_sec             number > 0   interval between health/session/adb polls
    startup_timeout_sec  number > 0   max seconds to wait for RECORDING
    package_name         string       Android package used by force_stop/launch
    faults               array        of fault objects:
        at_sec        number >= 0   seconds it becomes due
        serial        string        phone serial as shown by `adb devices`
        action        string        one of the allowed safe actions (below)
        duration_sec  optional positive number; ONLY valid for wifi_off, and
                       schedules an automatic wifi_on restart at at_sec+duration_sec

Allowed safe ADB actions (anything else is rejected at plan load):
    wifi_off    adb -s <serial> shell svc wifi disable
    wifi_on     adb -s <serial> shell svc wifi enable
    screen_off  adb -s <serial> shell input keyevent 223    (KEYCODE_SLEEP; may be unavailable on locked-down builds)
    screen_on   adb -s <serial> shell input keyevent 224    (KEYCODE_WAKEUP; may be unavailable on locked-down builds)
    force_stop  adb -s <serial> shell am force-stop <package_name>
    launch      adb -s <serial> shell monkey -p <package_name>
                            -c android.intent.category.LAUNCHER 1

Evidence is written under the (timestamped) run directory:
    run.json, health.jsonl, session.jsonl, adb.jsonl, events.jsonl, summary.json
Every record carries a timezone-aware ISO timestamp plus epoch milliseconds. Records
are flushed per observation, so evidence survives Ctrl+C and all other exits.

Exit codes:
    0  normal completion (dry-run, planned duration elapsed, session ended, or
       interrupted after recording was observed)
    1  planned recording never started (--wait-for-recording startup timeout, or
       interrupted while still waiting), or a configured fault serial was not
       online when monitoring began
    2  configuration error: unreadable/invalid plan, invalid timing, or the run
       directory could not be created

Examples (Windows PowerShell):
    python tools/soak_monitor.py --help
    python tools/soak_monitor.py --dry-run --plan plan.json --run-dir out/dry
    python tools/soak_monitor.py --plan plan.json --backend http://192.168.1.20:8000
    python tools/soak_monitor.py --plan plan.json --no-wait-for-recording
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

DEFAULT_BACKEND = "http://127.0.0.1:8000"
DEFAULT_PACKAGE_NAME = "com.example.sensors_app"
DEFAULT_RUN_ROOT = "soak_runs"
RECORDING_STATE = "RECORDING"

HTTP_TIMEOUT_SEC = 8.0
ADB_SNAPSHOT_TIMEOUT_SEC = 30.0
ADB_CMD_TIMEOUT_SEC = 30.0
TAIL_MAX_CHARS = 1000
SCREEN_INJECTION_DENIED = "INJECT_EVENTS permission"

# Actions that don't need a package name, keyed by action name.
SAFE_ACTIONS: dict[str, list[str]] = {
    "wifi_off": ["shell", "svc", "wifi", "disable"],
    "wifi_on": ["shell", "svc", "wifi", "enable"],
    "screen_off": ["shell", "input", "keyevent", "223"],   # KEYCODE_SLEEP
    "screen_on": ["shell", "input", "keyevent", "224"],    # KEYCODE_WAKEUP
}

# Actions whose arguments need the configured package name.
PACKAGE_ACTIONS: dict[str, list[str]] = {
    "force_stop": ["shell", "am", "force-stop", "{package}"],
    "launch": ["shell", "monkey", "-p", "{package}",
               "-c", "android.intent.category.LAUNCHER", "1"],
}

ALL_ACTIONS: frozenset[str] = frozenset(SAFE_ACTIONS) | frozenset(PACKAGE_ACTIONS)


class PlanError(ValueError):
    """Raised when the plan file is unreadable or violates the allowed schema."""


@dataclass
class Fault:
    index: int
    at_sec: float
    serial: str
    action: str
    duration_sec: float | None = None
    source_index: int | None = None
    # Runtime result fields, filled in as the fault is executed or skipped.
    executed: bool = False
    skipped: bool = False
    skipped_reason: str | None = None
    command: list[str] | None = field(default=None, repr=False)
    returncode: int | None = None
    timed_out: bool = False
    error: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


# ── Plan loading & validation (all rejects happen before the monitor begins) ──

def _as_positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{name} must be a number")
    num = float(value)
    if not math.isfinite(num) or num <= 0:
        raise PlanError(f"{name} must be a positive number")
    return num


def _validate_faults(raw_faults: object) -> list[Fault]:
    if not isinstance(raw_faults, list):
        raise PlanError("plan key 'faults' must be a list")
    faults: list[Fault] = []
    for i, raw in enumerate(raw_faults):
        if not isinstance(raw, dict):
            raise PlanError(f"faults[{i}] must be an object")
        at_sec = raw.get("at_sec")
        if (isinstance(at_sec, bool) or not isinstance(at_sec, (int, float))
                or not math.isfinite(float(at_sec)) or float(at_sec) < 0):
            raise PlanError(f"faults[{i}].at_sec must be a number >= 0")
        serial = raw.get("serial")
        if not isinstance(serial, str) or not serial.strip():
            raise PlanError(f"faults[{i}].serial must be a non-empty string")
        action = raw.get("action")
        if action not in ALL_ACTIONS:
            raise PlanError(
                f"faults[{i}].action {action!r} is not allowed "
                f"(allowed: {', '.join(sorted(ALL_ACTIONS))})")
        duration_sec = raw.get("duration_sec")
        if duration_sec is not None:
            if (isinstance(duration_sec, bool)
                    or not isinstance(duration_sec, (int, float))
                    or not math.isfinite(float(duration_sec))
                    or float(duration_sec) <= 0):
                raise PlanError(f"faults[{i}].duration_sec must be positive when present")
            if action != "wifi_off":
                raise PlanError(
                    f"faults[{i}].duration_sec is only valid for action wifi_off")
        faults.append(Fault(
            index=i,
            at_sec=float(at_sec),
            serial=serial.strip(),
            action=action,
            duration_sec=float(duration_sec) if duration_sec is not None else None,
        ))
    return faults


def _synthesize_wifi_on(faults: list[Fault], duration_sec: float) -> list[Fault]:
    """Expand `wifi_off` + duration_sec into an automatic `wifi_on` restart.

    Raises PlanError if the synthesized restart lands after the planned run
    duration, because it could then never execute within the monitored window.
    """
    expanded = list(faults)
    for f in faults:
        if f.action == "wifi_off" and f.duration_sec is not None:
            restart_at = f.at_sec + f.duration_sec
            if restart_at > duration_sec:
                raise PlanError(
                    f"faults[{f.index}].wifi_off duration_sec ends at "
                    f"{restart_at:g}s, after the plan duration_sec of "
                    f"{duration_sec:g}s; the synthesized wifi_on restart would "
                    f"never execute")
            expanded.append(Fault(
                index=len(expanded),
                at_sec=restart_at,
                serial=f.serial,
                action="wifi_on",
                source_index=f.index,
            ))
    return expanded


def load_and_validate_plan(path: str | Path) -> tuple[dict, list[Fault]]:
    """Read and validate a plan file. Returns (plan, faults).

    Raises PlanError on any structural or semantic violation before anything else
    runs, so a bad plan can never reach fault execution.
    """
    plan_path = Path(path)
    if not plan_path.is_file():
        raise PlanError(f"plan file not found: {plan_path}")
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read plan file {plan_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError("plan must be a JSON object")

    plan = {
        "duration_sec": _as_positive_number(raw.get("duration_sec"), "duration_sec"),
        "poll_sec": _as_positive_number(raw.get("poll_sec"), "poll_sec"),
        "startup_timeout_sec": _as_positive_number(raw.get("startup_timeout_sec"),
                                                   "startup_timeout_sec"),
    }
    package_name = raw.get("package_name")
    if package_name is None:
        package_name = DEFAULT_PACKAGE_NAME
    if not isinstance(package_name, str) or not package_name.strip():
        raise PlanError("package_name must be a non-empty string when present")
    plan["package_name"] = package_name.strip()
    if "faults" not in raw:
        raise PlanError("plan is missing required key 'faults'")
    faults = _validate_faults(raw["faults"])
    faults = _synthesize_wifi_on(faults, plan["duration_sec"])
    return plan, faults


# ── Subprocess/HTTP helpers (argument lists only, never shell=True) ──────────

def _http_get(backend: str, path: str) -> tuple[int | None, dict | None, str | None]:
    """GET `backend + path`. Returns (status, parsed_json, error)."""
    url = backend.rstrip("/") + path
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            status = resp.status
            body = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError) as exc:
        return None, None, f"{exc.__class__.__name__}: {exc}"
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return status, None, f"non-JSON response: {exc}"
    return status, data, None


def _adb_devices(adb_bin: str) -> tuple[list[dict], str | None]:
    """Run `adb devices -l` and return (parsed_devices, error_or_None)."""
    try:
        proc = subprocess.run([adb_bin, "devices", "-l"], capture_output=True,
                              text=True, timeout=ADB_SNAPSHOT_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{exc.__class__.__name__}: {exc}"
    if proc.returncode != 0:
        return [], f"exit {proc.returncode}: {_tail(proc.stderr)}"
    devices: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0]:
            entry: dict = {"serial": parts[0], "state": parts[1]}
            for token in parts[2:]:
                if ":" in token:
                    key, _, value = token.partition(":")
                    entry[key] = value
            devices.append(entry)
    return devices, None


def _serial_is_online(devices: list[dict], serial: str) -> bool:
    return any(d.get("serial") == serial and d.get("state") == "device"
               for d in devices)


def _tail(text: str | None, max_chars: int = TAIL_MAX_CHARS) -> str:
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars] + f"...[truncated {len(stripped) - max_chars} chars]"


def _build_command(fault: Fault, package_name: str, adb_bin: str = "adb") -> list[str]:
    """Build the argument list for a fault, always with -s <serial> and no shell."""
    if fault.action in SAFE_ACTIONS:
        args = SAFE_ACTIONS[fault.action]
    else:
        args = [a.format(package=package_name) for a in PACKAGE_ACTIONS[fault.action]]
    return [adb_bin, "-s", fault.serial] + args


def _resolve_run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(DEFAULT_RUN_ROOT) / f"soak_{stamp}"


# ── Run evidence writer ───────────────────────────────────────────────────────

class SoakRunner:
    """Runs the soak observation loop and writes all evidence files."""

    def __init__(self, args: argparse.Namespace, plan: dict, faults: list[Fault],
                 run_dir: Path) -> None:
        self.args = args
        self.plan = plan
        self.faults = faults
        self.run_dir = run_dir
        self.backend = args.backend.rstrip("/")
        self.adb = args.adb
        self.dry_run = args.dry_run
        self.wait_for_recording = args.wait_for_recording
        self.stop_after_planned = args.stop_after_planned
        self.duration_sec = plan["duration_sec"]
        self.poll_sec = plan["poll_sec"]
        self.startup_timeout_sec = plan["startup_timeout_sec"]
        self.package_name = plan["package_name"]

        self.started_iso = _now_iso()
        self.started_epoch_ms = _now_epoch_ms()
        self.reference_mono: float | None = None   # wall-clock anchor for scheduling
        self.recording_start_mono: float | None = None
        self.recording_start_iso: str | None = None
        self.recording_start_epoch_ms: int | None = None
        self.n_health = 0
        self.n_session = 0
        self.n_adb = 0
        self.n_http_errors = 0
        self.degraded = False
        self._last_devices: list[dict] = []
        self._handles: dict[str, TextIO] = self._open_writers()

    def _open_writers(self) -> dict[str, TextIO]:
        names = ("health.jsonl", "session.jsonl", "adb.jsonl", "events.jsonl")
        handles: dict[str, TextIO] = {}
        for name in names:
            handles[name] = (self.run_dir / name).open(
                "w", encoding="utf-8", newline="\n")
        return handles

    # -- low-level record helpers ---------------------------------------------

    def _write_json(self, path: Path, obj: dict) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def _write_jsonl(self, handle: TextIO, record: dict) -> None:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()

    def _log_event(self, level: str, event: str, detail: dict) -> None:
        self._write_jsonl(self._handles["events.jsonl"], {
            "ts_iso": _now_iso(),
            "ts_ms": _now_epoch_ms(),
            "level": level,
            "event": event,
            "detail": detail,
        })

    def _start(self) -> None:
        self._log_event("INFO", "run_start", {
            "backend": self.backend,
            "dry_run": self.dry_run,
            "wait_for_recording": self.wait_for_recording,
            "stop_after_planned": self.stop_after_planned,
        })
        self._write_run_json()

    def _write_run_json(self) -> None:
        meta = {
            "schema_version": 1,
            "tool": "tools/soak_monitor.py",
            "run_id": self.run_dir.name,
            "backend": self.backend,
            "adb": self.adb,
            "dry_run": self.dry_run,
            "wait_for_recording": self.wait_for_recording,
            "stop_after_planned": self.stop_after_planned,
            "started_iso": self.started_iso,
            "started_epoch_ms": self.started_epoch_ms,
            "plan": self.plan,
            "intended_actions": [
                {
                    "index": f.index,
                    "at_sec": f.at_sec,
                    "serial": f.serial,
                    "action": f.action,
                    "duration_sec": f.duration_sec,
                    "source_index": f.source_index,
                    "command": _build_command(f, self.package_name, self.adb),
                }
                for f in self.faults
            ],
        }
        self._write_json(self.run_dir / "run.json", meta)

    # -- backend/adb polling ---------------------------------------------------

    def _poll_health(self) -> tuple[bool, dict | None]:
        status, data, err = _http_get(self.backend, "/health")
        record = {"ts_iso": _now_iso(), "ts_ms": _now_epoch_ms(),
                  "ok": err is None, "status": status}
        if err is None:
            record["response"] = data
        else:
            record["error"] = err
            self.n_http_errors += 1
            self.degraded = True
            self._log_event("WARN", "http_error", {"endpoint": "/health", "error": err})
        self._write_jsonl(self._handles["health.jsonl"], record)
        self.n_health += 1
        return err is None, data

    def _poll_session(self) -> tuple[bool, dict | None]:
        status, data, err = _http_get(self.backend, "/session")
        record = {"ts_iso": _now_iso(), "ts_ms": _now_epoch_ms(),
                  "ok": err is None, "status": status}
        if err is None:
            record["response"] = data
        else:
            record["error"] = err
            self.n_http_errors += 1
            self.degraded = True
            self._log_event("WARN", "http_error", {"endpoint": "/session", "error": err})
        self._write_jsonl(self._handles["session.jsonl"], record)
        self.n_session += 1
        return err is None, data

    def _snapshot_adb(self) -> None:
        devices, err = _adb_devices(self.adb)
        self._last_devices = devices
        record = {"ts_iso": _now_iso(), "ts_ms": _now_epoch_ms(),
                  "ok": err is None, "devices": devices, "error": err}
        if err is not None:
            self.degraded = True
            self._log_event("WARN", "adb_error", {"error": err})
        self._write_jsonl(self._handles["adb.jsonl"], record)
        self.n_adb += 1

    # -- fault execution -------------------------------------------------------

    def _preflight_serials(self) -> list[str]:
        """Return configured fault serials that are not online right now.

        Called once after the initial ADB snapshot, before waiting or scheduling,
        so a missing/offline phone is rejected up front rather than discovered
        mid-run. Dry-run never reaches this check.
        """
        missing = sorted({f.serial for f in self.faults}
                         - {d["serial"] for d in self._last_devices
                            if d.get("state") == "device"})
        return missing

    def _mark_recording_started(self, at_mono: float) -> None:
        self.recording_start_mono = at_mono
        self.recording_start_epoch_ms = _now_epoch_ms()
        self.recording_start_iso = _now_iso()
        if self.wait_for_recording:
            self.reference_mono = at_mono
        self._log_event("INFO", "recording_started", {
            "recording_start_iso": self.recording_start_iso,
            "recording_start_epoch_ms": self.recording_start_epoch_ms,
        })

    def _execute_fault(self, fault: Fault) -> None:
        fault.command = _build_command(fault, self.package_name, self.adb)
        self._log_event("INFO", "fault_start", {
            "fault_index": fault.index,
            "at_sec": fault.at_sec,
            "serial": fault.serial,
            "action": fault.action,
            "command": " ".join(fault.command),
        })
        if not _serial_is_online(self._last_devices, fault.serial):
            fault.skipped = True
            fault.skipped_reason = "serial not present in adb devices"
            self.degraded = True
            self._log_event("WARN", "fault_skipped", {
                "fault_index": fault.index,
                "serial": fault.serial,
                "action": fault.action,
                "reason": fault.skipped_reason,
            })
            return
        try:
            proc = subprocess.run(fault.command, capture_output=True, text=True,
                                  timeout=ADB_CMD_TIMEOUT_SEC)
            fault.returncode = proc.returncode
            fault.stdout_tail = _tail(proc.stdout)
            fault.stderr_tail = _tail(proc.stderr)
        except subprocess.TimeoutExpired as exc:
            fault.timed_out = True
            fault.stderr_tail = _tail(exc.stderr)
        except OSError as exc:
            fault.error = f"{exc.__class__.__name__}: {exc}"
        fault.executed = True
        ok = (fault.error is None and not fault.timed_out
              and fault.returncode == 0)
        if (fault.action in {"screen_off", "screen_on"} and not ok
                and SCREEN_INJECTION_DENIED in fault.stderr_tail):
            # Some production Android builds reject shell input injection even over an
            # authorised ADB connection. Report this as an unsupported test action rather
            # than a device/app failure; the recording remains fully monitored and the
            # evidence explicitly shows that the screen transition was not exercised.
            fault.skipped = True
            fault.executed = False
            fault.skipped_reason = (
                "screen input injection unavailable on this Android build: "
                f"{SCREEN_INJECTION_DENIED}")
            self._log_event("WARN", "fault_skipped", {
                "fault_index": fault.index,
                "serial": fault.serial,
                "action": fault.action,
                "reason": fault.skipped_reason,
                "command": " ".join(fault.command),
            })
            return
        if not ok:
            self.degraded = True
        self._log_event("INFO" if ok else "ERROR", "fault_result", {
            "fault_index": fault.index,
            "serial": fault.serial,
            "action": fault.action,
            "command": " ".join(fault.command),
            "returncode": fault.returncode,
            "timed_out": fault.timed_out,
            "error": fault.error,
            "stdout": fault.stdout_tail,
            "stderr": fault.stderr_tail,
            "ok": ok,
        })

    # -- main loops ------------------------------------------------------------

    def run_live(self) -> tuple[str, int]:
        loop_start = time.monotonic()
        if not self.wait_for_recording:
            # Absolute schedule: faults are relative to monitor start.
            self.reference_mono = loop_start
        self._snapshot_adb()
        missing = self._preflight_serials()
        if missing:
            self.degraded = True
            self._log_event("ERROR", "preflight_serials_missing", {
                "missing_serials": missing,
                "reason": "configured fault serial not online before monitoring",
            })
            return "preflight_serials_missing", 1
        next_cycle_at = time.monotonic()
        while True:
            now = time.monotonic()
            if next_cycle_at > now:
                step = min(self.poll_sec * 0.25, next_cycle_at - now)
                time.sleep(step if step > 0 else 0.01)
                continue
            next_cycle_at = now + self.poll_sec
            cycle_start = time.monotonic()

            if (self.wait_for_recording and self.recording_start_mono is None):
                waited = cycle_start - loop_start
                if waited >= self.startup_timeout_sec:
                    self._log_event("ERROR", "startup_timeout", {
                        "waited_sec": round(waited, 3),
                        "startup_timeout_sec": self.startup_timeout_sec,
                    })
                    return "startup_timeout", 1

            health_ok, health_data = self._poll_health()
            _, session_data = self._poll_session()
            self._snapshot_adb()

            state: object = None
            if health_ok and isinstance(health_data, dict):
                state = health_data.get("session_state")
            if state is None and isinstance(session_data, dict):
                state = session_data.get("state")

            if self.recording_start_mono is None and state == RECORDING_STATE:
                self._mark_recording_started(cycle_start)

            if self.reference_mono is not None:
                elapsed = cycle_start - self.reference_mono
                for fault in self.faults:
                    if fault.executed or fault.skipped or fault.at_sec > elapsed:
                        continue
                    self._execute_fault(fault)

                if self.stop_after_planned and elapsed >= self.duration_sec:
                    self._log_event("INFO", "run_complete", {
                        "reason": "planned_duration_elapsed",
                        "elapsed_sec": round(elapsed, 3),
                    })
                    return "planned_duration_elapsed", 0

                if (self.wait_for_recording and self.recording_start_mono is not None
                        and state is not None and state != RECORDING_STATE):
                    self._log_event("INFO", "run_complete", {
                        "reason": "session_ended",
                        "observed_state": state,
                    })
                    return "session_ended", 0

    def run_dry_run(self) -> tuple[str, int]:
        """Validate-and-plan only: log intended ADB actions, never execute them."""
        for fault in self.faults:
            cmd = _build_command(fault, self.package_name, self.adb)
            fault.command = cmd
            self._log_event("INFO", "fault_planned", {
                "fault_index": fault.index,
                "at_sec": fault.at_sec,
                "serial": fault.serial,
                "action": fault.action,
                "command": " ".join(cmd),
            })
        return "dry_run_complete", 0

    def finish_reason_for_interrupt(self) -> tuple[str, int]:
        code = 1 if (self.wait_for_recording
                     and self.recording_start_mono is None) else 0
        return "interrupted", code

    # -- summary & teardown ----------------------------------------------------

    def close(self, exit_reason: str, exit_code: int) -> None:
        try:
            self._write_summary(exit_reason, exit_code)
        finally:
            for handle in self._handles.values():
                try:
                    handle.close()
                except OSError:
                    pass

    def _write_summary(self, exit_reason: str, exit_code: int) -> None:
        elapsed = None
        if self.reference_mono is not None:
            elapsed = round(time.monotonic() - self.reference_mono, 3)
        summary = {
            "schema_version": 1,
            "run_id": self.run_dir.name,
            "backend": self.backend,
            "dry_run": self.dry_run,
            "wait_for_recording": self.wait_for_recording,
            "stop_after_planned": self.stop_after_planned,
            "started_iso": self.started_iso,
            "started_epoch_ms": self.started_epoch_ms,
            "ended_iso": _now_iso(),
            "ended_epoch_ms": _now_epoch_ms(),
            "recording_start_iso": self.recording_start_iso,
            "recording_start_epoch_ms": self.recording_start_epoch_ms,
            "recording_elapsed_sec": elapsed,
            "plan": self.plan,
            "observations": {
                "health": self.n_health,
                "session": self.n_session,
                "adb_snapshots": self.n_adb,
                "http_errors": self.n_http_errors,
            },
            "faults_executed": sum(1 for f in self.faults if f.executed),
            "faults_skipped": sum(1 for f in self.faults if f.skipped),
            "faults": [_fault_summary(f) for f in self.faults],
            "last_adb_devices": self._last_devices,
            "degraded": self.degraded,
            "exit_reason": exit_reason,
            "exit_code": exit_code,
        }
        self._write_json(self.run_dir / "summary.json", summary)


def _fault_summary(fault: Fault) -> dict:
    status = ("executed" if fault.executed
              else "skipped" if fault.skipped else "pending")
    return {
        "index": fault.index,
        "at_sec": fault.at_sec,
        "serial": fault.serial,
        "action": fault.action,
        "duration_sec": fault.duration_sec,
        "source_index": fault.source_index,
        "status": status,
        "skipped_reason": fault.skipped_reason,
        "command": fault.command,
        "returncode": fault.returncode,
        "timed_out": fault.timed_out,
        "error": fault.error,
        "stdout_tail": fault.stdout_tail,
        "stderr_tail": fault.stderr_tail,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="soak_monitor.py",
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--plan", required=True,
                   help="path to the JSON plan file (see schema above)")
    p.add_argument("--run-dir", default=None,
                   help=f"output directory (default: {DEFAULT_RUN_ROOT}/soak_<timestamp>)")
    p.add_argument("--backend", default=DEFAULT_BACKEND,
                   help=f"backend base URL (default: {DEFAULT_BACKEND})")
    wait_group = p.add_mutually_exclusive_group()
    wait_group.add_argument("--wait-for-recording", dest="wait_for_recording",
                            action="store_true",
                            help="wait for backend session_state RECORDING before "
                                 "scheduling faults/duration (default)")
    wait_group.add_argument("--no-wait-for-recording", dest="wait_for_recording",
                            action="store_false",
                            help="start the fault schedule immediately, using "
                                 "monitor start as the reference time")
    p.set_defaults(wait_for_recording=True)
    p.add_argument("--dry-run", action="store_true",
                   help="validate the plan, create the evidence files, and log "
                        "intended ADB actions without executing them or touching "
                        "the backend")
    p.add_argument("--stop-after-duration", "--stop-after-planned",
                   dest="stop_after_planned",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="stop monitoring once the planned duration_sec has elapsed "
                        "since recording start (default). "
                        "--no-stop-after-duration keeps monitoring until the "
                        "session ends or the monitor is interrupted")
    p.add_argument("--adb", default="adb",
                   help="adb executable (default: 'adb' on PATH)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        plan, faults = load_and_validate_plan(args.plan)
    except PlanError as exc:
        print(f"error: invalid plan: {exc}", file=sys.stderr)
        return 2

    run_dir = _resolve_run_dir(args)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create run directory {run_dir}: {exc}", file=sys.stderr)
        return 2

    try:
        runner = SoakRunner(args=args, plan=plan, faults=faults, run_dir=run_dir)
    except OSError as exc:
        print(f"error: cannot prepare run directory {run_dir}: {exc}", file=sys.stderr)
        return 2

    exit_reason = "internal_error"
    exit_code = 2
    try:
        runner._start()
        if args.dry_run:
            exit_reason, exit_code = runner.run_dry_run()
        else:
            exit_reason, exit_code = runner.run_live()
    except KeyboardInterrupt:
        exit_reason, exit_code = runner.finish_reason_for_interrupt()
    finally:
        try:
            runner.close(exit_reason, exit_code)
        except Exception as exc:  # noqa: BLE001 — evidence first, never mask the real reason
            print(f"error writing summary: {exc}", file=sys.stderr)
            if exit_code == 0:
                exit_code = 2

    print(f"run dir: {run_dir}")
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        print(f"summary: {summary_path}")
    if exit_code != 0:
        print(f"soak monitor failed ({exit_reason})", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
