"""Service wiring for the v3 API: the Runtime, the notice store and the draft store.

One :class:`Runtime` object is created in the app lifespan and holds everything the
v3 endpoints and ``/ws/ui`` share: the camera service (behind the L2
``CameraService`` seam), the server-side notice store, the shared Setup-form draft
and the UI hub.  Legacy modules keep their own singletons untouched; this is purely
additive.

State files live in ``SSD_PATH/Data_Riset_IMU/_state/`` and are written the way
``cameras.py`` writes its anchors — tmp file, fsync, ``os.replace`` — so a crash can
never leave a half-written store:

* ``notices.json`` — every notice ever raised (resolved/dismissed ones stay for
  history; the snapshot only lists live ones);
* ``draft.json``   — the Setup form shared by every dashboard;
* ``sessions/<sid>.post.json`` — post-session job records (see ``post_session.py``).

``SSD_PATH`` is a module attribute resolved at import, same pattern as
``export.py``/``cameras.py``; the test rig monkeypatches it, and ``state_root()``
re-reads it on every call so redirection works without a reload.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import Draft, Notice

logger = logging.getLogger(__name__)

SSD_PATH = Path(os.getenv("SSD_PATH", "./data"))

#: Notice thresholds from L4 spec §3: below 20 GB warns, below 5 GB errors (and 5 GB
#: is also a START blocker in the readiness rules).
DISK_WARN_BYTES = 20 * 1024**3
DISK_ERROR_BYTES = 5 * 1024**3

_NOTICE_LEVELS = ("info", "warn", "error")


# ---------------------------------------------------------------------------
# state files
# ---------------------------------------------------------------------------

def state_root() -> Path:
    """``SSD_PATH/Data_Riset_IMU/_state`` — resolved per call, monkeypatch-friendly."""
    return SSD_PATH / "Data_Riset_IMU" / "_state"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path, obj: Any) -> None:
    """tmp + fsync + rename, the durability pattern cameras.py already uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# notice store
# ---------------------------------------------------------------------------

class NoticeStore:
    """Durable operator-facing notices (``_state/notices.json``).

    ``raise_`` is idempotent on ``key``: re-raising an unresolved keyed notice
    updates its message/level instead of stacking duplicates (a disk warning that
    re-fires every 5 s must stay one notice).  A *resolved* key raised again creates
    a fresh notice — the problem came back and deserves a new timestamp.  ``key`` is
    also how sources withdraw a notice: ``resolve(key)`` when the condition clears.
    ``dismiss(id)`` is the operator's "stop showing me this" and survives restarts.
    """

    def __init__(self, root: Callable[[], Path]) -> None:
        self._root = root
        self._records: list[dict] = []
        self._listeners: list[Callable[[], None]] = []
        loaded = read_json(self._path(), [])
        if isinstance(loaded, list):
            self._records = [r for r in loaded if isinstance(r, dict) and r.get("id")]

    def _path(self) -> Path:
        return self._root() / "notices.json"

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Called (sync) after any visible change, so the UI hub can coalesce."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as exc:  # a listener must never break the store
                logger.debug("notice listener failed: %s", exc)

    def _commit(self) -> None:
        atomic_write_json(self._path(), self._records)
        self._notify()

    def raise_(
        self,
        code: str,
        level: str,
        message: str,
        session_id: str | None = None,
        key: str | None = None,
    ) -> dict:
        """Raise (or update) a notice. Returns the record."""
        if level not in _NOTICE_LEVELS:
            raise ValueError(f"bad notice level {level!r}")
        if key is not None:
            for record in reversed(self._records):
                if record.get("key") == key and not record.get("resolved"):
                    changed = (
                        record.get("message") != message
                        or record.get("level") != level
                        or (session_id is not None and record.get("session_id") != session_id)
                    )
                    record["message"] = message
                    record["level"] = level
                    if session_id is not None:
                        record["session_id"] = session_id
                    if changed:
                        self._commit()
                    return record
        record = {
            "id": uuid.uuid4().hex,
            "level": level,
            "code": code,
            "message": message,
            "session_id": session_id,
            "created_at_ms": int(time.time() * 1000),
            "resolved": False,
            "dismissed_at_ms": None,
            "key": key,
        }
        self._records.append(record)
        self._commit()
        return record

    def resolve(self, key: str) -> int:
        """Mark every unresolved notice with this key resolved. Returns the count."""
        count = 0
        for record in self._records:
            if record.get("key") == key and not record.get("resolved"):
                record["resolved"] = True
                count += 1
        if count:
            self._commit()
        return count

    def dismiss(self, notice_id: str) -> bool:
        """Operator dismissal: hides the notice without claiming the problem is gone."""
        for record in self._records:
            if record.get("id") == notice_id:
                if record.get("dismissed_at_ms") is None:
                    record["dismissed_at_ms"] = int(time.time() * 1000)
                    self._commit()
                return True
        return False

    def get(self, notice_id: str) -> dict | None:
        for record in self._records:
            if record.get("id") == notice_id:
                return record
        return None

    def _live(self, record: dict) -> bool:
        return not record.get("resolved") and record.get("dismissed_at_ms") is None

    def snapshot(self) -> list[Notice]:
        """Unresolved, undismissed notices for the UI snapshot, oldest first."""
        return [
            Notice(
                id=str(r["id"]),
                level=r.get("level", "warn"),
                code=str(r.get("code", "")),
                message=str(r.get("message", "")),
                session_id=r.get("session_id"),
                created_at_ms=int(r.get("created_at_ms", 0) or 0),
                resolved=bool(r.get("resolved", False)),
            )
            for r in self._records
            if self._live(r)
        ]

    def count_for_session(self, session_id: str) -> int:
        """Live notices attached to one session (``SessionSummary.notices``)."""
        return sum(
            1 for r in self._records
            if self._live(r) and r.get("session_id") == session_id
        )


# ---------------------------------------------------------------------------
# draft store
# ---------------------------------------------------------------------------

class DraftStore:
    """Server-side Setup form (``_state/draft.json``), shared by every dashboard.

    ``PUT /api/session/draft`` merges: a field the client omits (None) keeps its
    value.  After a successful START only ``subject`` is cleared — tag and operator
    stay because the same operator usually records several sessions in a row.
    """

    FIELDS = ("subject", "session_tag", "operator")

    def __init__(self, root: Callable[[], Path]) -> None:
        self._root = root
        self._listeners: list[Callable[[], None]] = []
        loaded = read_json(self._path(), {})
        self._data: dict = {name: "" for name in self.FIELDS}
        self._data["updated_at_ms"] = 0
        if isinstance(loaded, dict):
            for name in self.FIELDS:
                self._data[name] = str(loaded.get(name, "") or "")
            try:
                self._data["updated_at_ms"] = int(loaded.get("updated_at_ms", 0) or 0)
            except (TypeError, ValueError):
                self._data["updated_at_ms"] = 0

    def _path(self) -> Path:
        return self._root() / "draft.json"

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as exc:
                logger.debug("draft listener failed: %s", exc)

    def _commit(self) -> None:
        atomic_write_json(self._path(), self._data)
        self._notify()

    def get(self) -> Draft:
        return Draft(
            subject=self._data["subject"],
            session_tag=self._data["session_tag"],
            operator=self._data["operator"],
            updated_at_ms=self._data["updated_at_ms"],
        )

    def update(
        self,
        subject: str | None = None,
        session_tag: str | None = None,
        operator: str | None = None,
    ) -> Draft:
        """Merge the provided fields, stamp ``updated_at_ms`` and persist."""
        for name, value in (
            ("subject", subject), ("session_tag", session_tag), ("operator", operator)
        ):
            if value is not None:
                self._data[name] = str(value)
        self._data["updated_at_ms"] = int(time.time() * 1000)
        self._commit()
        return self.get()

    def clear_subject(self) -> None:
        """Post-START reset: the subject is consumed, tag/operator are reused."""
        if self._data["subject"]:
            self._data["subject"] = ""
            self._data["updated_at_ms"] = int(time.time() * 1000)
            self._commit()


# ---------------------------------------------------------------------------
# camera service factory
# ---------------------------------------------------------------------------

def create_camera_service() -> Any:
    """Build the ``CameraService`` chosen by ``IMU_CAMERAS`` (auto|fake|stub|off).

    * ``auto`` — the real ffmpeg-backed service on the host's cameras.
    * ``fake`` — the same real service, but discovery yields ``IMU_FAKE_CAMERAS``
      synthetic lavfi cameras, so capture, encoding, previews and calibration all run
      for real without hardware (camera/discovery.py reads the same variable).
    * ``stub`` — the in-memory ``FakeCameraService`` test double: no ffmpeg, writes
      placeholder files. Unit tests of the session core use it.
    * ``off`` — the stub with no cameras.

    ``fake`` used to select the in-memory double while the camera service's own
    discovery read it as "synthetic cameras", so a "fake" run never exercised the
    video pipeline at all.
    """
    mode = (os.getenv("IMU_CAMERAS") or "auto").strip().lower()
    if mode == "stub":
        try:
            n = int(os.getenv("IMU_FAKE_CAMERAS", "2"))
        except ValueError:
            n = 2
        from ..camera.fake import FakeCameraService

        logger.info("camera service: in-memory stub with %d camera(s)", n)
        return FakeCameraService(n=n)
    if mode == "off":
        from ..camera.fake import FakeCameraService

        logger.info("camera service: off")
        return FakeCameraService(n=0)
    if mode not in ("auto", "fake"):
        logger.warning("unknown IMU_CAMERAS=%r — falling back to 'auto'", mode)
    try:
        from master_backend.app.camera.service import create_service
    except ImportError:
        from ..camera.fake import FakeCameraService

        logger.warning(
            "master_backend.app.camera.service is not importable (lane L5 absent); "
            "falling back to FakeCameraService(n=0) — camera capture disabled"
        )
        return FakeCameraService(n=0)
    return create_service()


def camera_router_or_none() -> Any:
    """L5's ``/api/cameras/*`` + ``/api/calibration/*`` router, if this branch has it."""
    try:
        from master_backend.app.camera.routes import router

        return router
    except Exception as exc:
        logger.info("camera router not mounted (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

@dataclass
class Runtime:
    """Everything the v3 layer shares, created once in the app lifespan."""

    camera_service: Any
    notices: NoticeStore
    drafts: DraftStore
    state_root: Callable[[], Path]
    #: UI hub (``ws_ui.py``) and post-session manager (``post_session.py``), attached
    #: after construction; both are optional so unit tests can build a bare Runtime.
    hub: Any | None = None
    post: Any | None = None
    #: Finished sessions the watchdog still watches for outstanding rescue uploads.
    recent_sessions: list[str] = field(default_factory=list)

    def note_session(self, session_id: str) -> None:
        if session_id and session_id not in self.recent_sessions:
            self.recent_sessions.append(session_id)
            del self.recent_sessions[:-16]


def create_runtime(
    *,
    root: Path | Callable[[], Path] | None = None,
    camera_service: Any | None = None,
) -> Runtime:
    """Build a Runtime. ``root`` overrides the state dir (tests); ``camera_service``
    overrides the env-driven factory (tests inject ``FakeCameraService``)."""
    if root is None:
        root_fn: Callable[[], Path] = state_root
    elif callable(root):
        root_fn = root
    else:
        fixed = Path(root)
        root_fn = lambda: fixed  # noqa: E731
    if camera_service is None:
        camera_service = create_camera_service()
    rt = Runtime(
        camera_service=camera_service,
        notices=NoticeStore(root_fn),
        drafts=DraftStore(root_fn),
        state_root=root_fn,
    )
    (root_fn() / "sessions").mkdir(parents=True, exist_ok=True)
    return rt


_runtime: Runtime | None = None


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("v3 runtime not initialised (app lifespan has not run)")
    return _runtime


# ---------------------------------------------------------------------------
# notice sources that live outside the post-session job
# ---------------------------------------------------------------------------

def wire_lifecycle(rt: Runtime) -> Callable[[], None]:
    """Hook the notice store into lifecycle events and camera anchors.

    Returns the teardown callable (the lifespan stores it; tests call it so nothing
    leaks into the next test's singleton).  Covers the notice sources that are not
    owned by the post-session job:

    * a FinalizeJob step failed (``FINALIZE_COMPLETE`` carries the step snapshot);
    * late delivery received (the idle reaper notifies ``LATE_DELIVERY``);
    * a camera reported ``track_ended``/``write_error`` (live, via the anchor hook —
      the post-session job additionally scans the stop result, same idempotent key).
    """
    from .. import cameras as cameras_module
    from ..session_manager import session_manager

    async def observer(event: dict) -> None:
        kind = event.get("type")
        sid = str(event.get("session_id") or "")
        if kind == "FINALIZE_COMPLETE":
            rt.note_session(sid)
            for step in (event.get("finalize") or {}).get("steps") or []:
                if step.get("state") == "failed":
                    rt.notices.raise_(
                        code="finalize_step_failed",
                        level="error",
                        message=(
                            f"Finalize step '{step.get('step')}' failed for session "
                            f"{sid}: {step.get('detail') or 'unknown error'}"
                        ),
                        session_id=sid,
                        key=f"finalize_failed:{sid}:{step.get('step')}",
                    )
        elif kind == "LATE_DELIVERY":
            devices = event.get("devices") or {}
            rows = sum(int(d.get("rows_appended", 0) or 0) for d in devices.values())
            rt.notices.raise_(
                code="late_delivery",
                level="info",
                message=(
                    f"Late telemetry for session {sid}: {rows} row(s) from "
                    f"{len(devices)} device(s) arrived after STOP."
                ),
                session_id=sid,
                key=f"late_delivery:{sid}",
            )

    def mark_hook(session_id: str, event: str, record: dict) -> None:
        if event not in ("track_ended", "write_error"):
            return
        cam_id = str(record.get("cam_id") or "?")
        rt.notices.raise_(
            code=f"camera_{event}",
            level="warn",
            message=(
                f"Camera {cam_id} reported '{event}' during session {session_id}; "
                "check the video for a gap."
            ),
            session_id=session_id,
            key=f"camera_event:{session_id}:{cam_id}:{event}",
        )

    session_manager.add_observer(observer)
    cameras_module.add_mark_hook(mark_hook)

    def unwire() -> None:
        with contextlib.suppress(ValueError):
            session_manager._observers.remove(observer)
        with contextlib.suppress(ValueError):
            cameras_module.remove_mark_hook(mark_hook)

    return unwire


def check_disk(rt: Runtime) -> None:
    """Raise/resolve the single ``disk_space`` notice from current free bytes."""
    anchor = rt.state_root()
    while not anchor.is_dir() and anchor != anchor.parent:
        anchor = anchor.parent
    if not anchor.is_dir():
        return
    free = shutil.disk_usage(anchor).free
    gib = free / 1024**3
    if free < DISK_ERROR_BYTES:
        rt.notices.raise_(
            code="disk_space",
            level="error",
            message=(
                f"Data disk ({anchor}) has only {gib:.1f} GB free — below the 5 GB "
                "floor. Recording is blocked; free space now."
            ),
            key="disk_space",
        )
    elif free < DISK_WARN_BYTES:
        rt.notices.raise_(
            code="disk_space",
            level="warn",
            message=(
                f"Data disk ({anchor}) has only {gib:.1f} GB free — below 20 GB. "
                "Consider exporting and clearing old sessions."
            ),
            key="disk_space",
        )
    else:
        rt.notices.resolve("disk_space")


def check_rescue_uploads(rt: Runtime) -> None:
    """Warn about unverified phone rescue uploads for finished sessions; resolve on
    completion. Only sessions this process saw finish (or resumed) are polled — an
    upload for an ancient session is the Sessions view's business, not a notice."""
    from ..export import _recovery_manifest

    for sid in list(rt.recent_sessions):
        for info in _recovery_manifest(sid):
            device = str(info.get("device_id") or "")
            key = f"rescue_outstanding:{sid}:{device}"
            if info.get("verified"):
                rt.notices.resolve(key)
                continue
            if info.get("done"):
                continue  # operator already dismissed this recovery in the legacy UI
            received = int(info.get("received_bytes", 0) or 0)
            total = int(info.get("total_bytes", 0) or 0)
            state = str(info.get("state") or ("complete" if info.get("complete") else "receiving"))
            rt.notices.raise_(
                code="rescue_outstanding",
                level="warn",
                message=(
                    f"Phone rescue upload for session {sid} ({device[:8]}…) is still "
                    f"{state}: {received} of {total} bytes, not verified."
                ),
                session_id=sid,
                key=key,
            )


async def watchdog_loop(rt: Runtime, interval: float = 5.0) -> None:
    """Background poll for the condition-style notice sources (disk, rescue uploads).

    Event-style sources (finalize failures, late delivery, camera events) fire from
    ``wire_lifecycle``; conditions that can also *clear* need a poll. Runs until the
    lifespan cancels it.
    """
    while True:
        await asyncio.sleep(interval)
        for check in (check_disk, check_rescue_uploads):
            try:
                check(rt)
            except Exception as exc:
                logger.debug("watchdog check %s failed: %s", check.__name__, exc)
