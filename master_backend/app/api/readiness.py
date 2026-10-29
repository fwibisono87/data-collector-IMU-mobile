"""START readiness: blockers (refuse START) and warnings (record and continue).

Merges the backend rules from L4 spec §6 with whatever the camera service reports, so the
Setup screen explains exactly why START is disabled and how to fix it. The v3 START
endpoint recomputes this rather than trusting the snapshot the dashboard saw: the phone
that was online when the page rendered may be gone by the time the button is pressed.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..session_manager import SessionState, session_manager
from .runtime import DISK_ERROR_BYTES, DISK_WARN_BYTES, Runtime
from .schema import Issue, Readiness

#: Block START when the recording would not be usable at the phone's measured rate.
MIN_TRUE_HZ = 90.0

#: Writability probes are tiny but the snapshot rebuild runs at up to 5 Hz; cache briefly.
_PROBE_TTL_S = 1.0
_probe_cache: dict[str, tuple[float, bool]] = {}


def _disk_anchor(rt: Runtime) -> Path:
    """The closest existing directory to the state root (the state dir may be new)."""
    anchor = rt.state_root()
    while not anchor.is_dir() and anchor != anchor.parent:
        anchor = anchor.parent
    return anchor


def _writable(anchor: Path) -> bool:
    key = str(anchor)
    now = time.monotonic()
    cached = _probe_cache.get(key)
    if cached is not None and now - cached[0] < _PROBE_TTL_S:
        return cached[1]
    probe = anchor / ".start_probe.tmp"
    try:
        probe.write_bytes(b"")
        ok = True
    except OSError:
        ok = False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    _probe_cache[key] = (now, ok)
    return ok


def _free_bytes(anchor: Path) -> int:
    try:
        return shutil.disk_usage(anchor).free
    except OSError:
        return 0


def _state_blocker() -> Issue | None:
    state = session_manager.state
    if state in (SessionState.PREFLIGHT, SessionState.READY, SessionState.IDLE):
        return None
    if state == SessionState.RECORDING:
        return Issue(code="already_recording",
                     message="A session is already recording. Stop it before starting another.")
    if state in (SessionState.FINALIZING, SessionState.VALIDATING):
        return Issue(code="finalizing",
                     message="The previous session is still finalising. Wait for it to finish.")
    return Issue(code="backend_error",
                 message="The backend is in an error state. Check the notices and restart it.")


def _disk_issues(anchor: Path, free: int) -> tuple[list[Issue], list[Issue]]:
    blockers: list[Issue] = []
    warnings: list[Issue] = []
    gib = free / 1024**3
    if not _writable(anchor):
        blockers.append(Issue(
            code="disk_not_writable",
            message=(
                f"Cannot write to the data disk ({anchor}). Connect the SSD or fix its "
                "permissions, then start again."
            ),
            subject=str(anchor),
        ))
    elif free < DISK_ERROR_BYTES:
        blockers.append(Issue(
            code="disk_full",
            message=(
                f"Only {gib:.1f} GB free on {anchor} — below the 5 GB minimum. Move or "
                "export old sessions before starting."
            ),
            subject=str(anchor),
        ))
    elif free < DISK_WARN_BYTES:
        warnings.append(Issue(
            code="disk_low",
            message=(
                f"Only {gib:.1f} GB free on {anchor} — below 20 GB. Consider exporting "
                "and clearing old sessions."
            ),
            subject=str(anchor),
        ))
    return blockers, warnings


def _phone_issues() -> tuple[list[Issue], list[Issue]]:
    blockers: list[Issue] = []
    warnings: list[Issue] = []
    phones = session_manager.online_devices
    if not phones:
        blockers.append(Issue(
            code="no_phone",
            message="No phone is online. Start the phone app and wait for it to connect.",
        ))
        return blockers, warnings

    for dev in phones:
        if dev.packets_received > 0 and dev.true_hz_avg < MIN_TRUE_HZ:
            warnings.append(Issue(
                code="phone_rate_low",
                message=(
                    f"{dev.device_role or dev.device_id} is streaming at "
                    f"{dev.true_hz_avg:.0f} Hz — below the {MIN_TRUE_HZ:.0f} Hz target. "
                    "The recording will be flagged in its integrity report."
                ),
                subject=dev.device_id,
            ))
    versions = {dev.app_version for dev in phones if dev.app_version}
    if len(versions) > 1:
        warnings.append(Issue(
            code="app_version_mismatch",
            message=(
                "Phones report different app versions "
                f"({', '.join(sorted(versions))}). Update them to the same build."
            ),
        ))
    return blockers, warnings


def _draft_issues(rt: Runtime) -> list[Issue]:
    draft = rt.drafts.get()
    labels = {
        "subject": "Subject",
        "session_tag": "Session tag",
        "operator": "Operator",
    }
    issues: list[Issue] = []
    for field, label in labels.items():
        if not str(getattr(draft, field) or "").strip():
            issues.append(Issue(
                code=f"{field}_missing",
                message=f"{label} is required before starting.",
                subject=field,
            ))
    return issues


def _camera_issues(rt: Runtime) -> tuple[list[Issue], list[Issue]]:
    try:
        blockers, warnings = rt.camera_service.readiness()
        return list(blockers), list(warnings)
    except Exception as exc:
        return [Issue(
            code="camera_unavailable",
            message=f"The camera service is not healthy: {exc}. Check the cameras and retry.",
        )], []


def compute_readiness(rt: Runtime) -> Readiness:
    anchor = _disk_anchor(rt)
    free = _free_bytes(anchor)
    disk_blockers, disk_warnings = _disk_issues(anchor, free)
    phone_blockers, phone_warnings = _phone_issues()
    camera_blockers, camera_warnings = _camera_issues(rt)

    blockers = list(disk_blockers) + phone_blockers + _draft_issues(rt) + camera_blockers
    state_blocker = _state_blocker()
    if state_blocker is not None:
        blockers.insert(0, state_blocker)
    warnings = disk_warnings + phone_warnings + camera_warnings
    return Readiness(blockers=blockers, warnings=warnings)