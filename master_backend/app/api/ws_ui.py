"""``/ws/ui`` — the dashboard's single push channel (API.md).

Two rules shape the implementation:

* every client has its own bounded queue (10 messages) and its own writer task, so one
  slow browser can never delay the others; when a queue overflows the OLDEST message is
  dropped (the next full snapshot always repairs whatever a dropped message carried);
* the server never patches: a snapshot is always whole, rebuilt from the singletons, so a
  reconnecting dashboard needs no reconciliation logic.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..session_manager import session_manager
from .. import ws_handler as legacy
from .post_session import session_progress
from .runtime import Runtime
from .schema import AckMsg, ErrorBody
from .snapshot import build_snapshot

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ui"])

SNAPSHOT_INTERVAL_S = 0.2     # ≤ 5 Hz, per API.md
LIVE_INTERVAL_S = 0.1         # 10 Hz
DRIFT_INTERVAL_S = 0.5        # 2 Hz
DRIFT_LINGER_S = 5.0
QUEUE_MAX = 10


class _Client:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.writer: asyncio.Task | None = None
        self.watch_ref: str | None = None

    def offer(self, message: dict) -> None:
        text = json.dumps(message)
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            # Drop the oldest: it is superseded by whatever we are sending now, and the
            # next whole snapshot makes any gap irrelevant.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(text)


class UiHub:
    """Owns ``/ws/ui`` clients and the background loops that feed them."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._clients: set[_Client] = set()
        self._loops: dict[str, asyncio.Task] = {}
        self._last_finalize: dict[str, str] = {}
        self._last_watch_at = 0.0
        self._unwire: list = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Listen to the stores and the session manager (called once by the lifespan)."""
        self.rt.notices.add_listener(self.mark_dirty)
        self.rt.drafts.add_listener(self.mark_dirty)
        session_manager.add_observer(self._on_session_event)
        self._unwire = [
            lambda: self._remove(self.rt.notices._listeners, self.mark_dirty),
            lambda: self._remove(self.rt.drafts._listeners, self.mark_dirty),
            lambda: self._remove(session_manager._observers, self._on_session_event),
        ]

    @staticmethod
    def _remove(items: list, item) -> None:
        with contextlib.suppress(ValueError):
            items.remove(item)

    async def close(self) -> None:
        for unwire in self._unwire:
            with contextlib.suppress(Exception):
                unwire()
        self._unwire = []
        for client in list(self._clients):
            if client.writer is not None:
                client.writer.cancel()
            with contextlib.suppress(Exception):
                await client.websocket.close()
        self._clients.clear()
        for task in list(self._loops.values()):
            task.cancel()
        for task in list(self._loops.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._loops.clear()

    def mark_dirty(self) -> None:
        """Nothing to coalesce explicitly: the snapshot loop polls at 5 Hz while clients
        exist. Kept as the listener target so stores need not know about the hub."""

    # -- events ------------------------------------------------------------

    async def _on_session_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind in ("FINALIZE_PROGRESS", "FINALIZE_COMPLETE"):
            session_id = str(event.get("session_id") or "")
            if session_id:
                await self.finalize_changed(session_id)
        # Every lifecycle event can change the snapshot (state, phones, progress).

    async def finalize_changed(self, session_id: str) -> None:
        """Publish one ``finalize`` message when the step set actually changed."""
        progress = session_progress(self.rt.post, session_id)
        if not progress:
            return
        signature = json.dumps(progress, sort_keys=True)
        if self._last_finalize.get(session_id) == signature:
            return
        self._last_finalize[session_id] = signature
        self._broadcast({"type": "finalize", "data": progress})

    # -- loops -------------------------------------------------------------

    def _ensure_loop(self, name: str, factory) -> None:
        task = self._loops.get(name)
        if task is None or task.done():
            self._loops[name] = asyncio.create_task(factory(), name=f"ws-ui-{name}")

    async def _snapshot_loop(self) -> None:
        while self._clients:
            try:
                snapshot = build_snapshot(self.rt)
            except Exception:
                logger.exception("snapshot build failed")
                await asyncio.sleep(SNAPSHOT_INTERVAL_S)
                continue
            payload = snapshot.model_dump(mode="json")
            # server_time_ms / telemetry_age_ms change every build, so this is a ≤5 Hz
            # stream while a client is connected — exactly the documented cadence.
            self._broadcast({"type": "snapshot", "data": payload})
            await asyncio.sleep(SNAPSHOT_INTERVAL_S)

    async def _live_loop(self) -> None:
        while self._clients:
            samples = dict(legacy.latest_samples())
            if samples:
                phones = {
                    device_id: {
                        "acc": sample.get("acc", [0.0, 0.0, 0.0]),
                        "gyro": sample.get("gyro", [0.0, 0.0, 0.0]),
                        "ts_ms": int(sample.get("ts", 0) or 0),
                    }
                    for device_id, sample in samples.items()
                }
                self._broadcast({
                    "type": "live",
                    "data": {"t_ms": int(time.time() * 1000), "phones": phones},
                })
            await asyncio.sleep(LIVE_INTERVAL_S)

    async def _drift_loop(self) -> None:
        service = self.rt.camera_service
        compare = getattr(service, "compare", None)
        while self._clients:
            watching = {c.watch_ref: [x for x in self._clients if x.watch_ref == c.watch_ref]
                        for c in self._clients if c.watch_ref}
            if not watching:
                if time.monotonic() - self._last_watch_at > DRIFT_LINGER_S:
                    return
                await asyncio.sleep(DRIFT_INTERVAL_S)
                continue
            self._last_watch_at = time.monotonic()
            for ref_id, clients in watching.items():
                try:
                    result = await compare(ref_id)
                except Exception as exc:
                    logger.debug("calibration compare(%s) failed: %s", ref_id, exc)
                    continue
                data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                message = {"type": "calibration.drift", "data": data}
                for client in clients:
                    client.offer(message)
                await asyncio.sleep(DRIFT_INTERVAL_S)

    # -- clients -----------------------------------------------------------

    def _broadcast(self, message: dict) -> None:
        for client in list(self._clients):
            client.offer(message)

    async def _writer(self, client: _Client) -> None:
        try:
            while True:
                text = await client.queue.get()
                await client.websocket.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _ack(self, client: _Client, req_id: str | None, ok: bool,
             code: str = "", message: str = "") -> None:
        if not req_id:
            return
        error = ErrorBody(code=code, message=message, details=None) if not ok else None
        client.offer(AckMsg(req_id=req_id, ok=ok, error=error).model_dump(mode="json"))

    async def _handle_client_message(self, client: _Client, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        req_id = message.get("req_id")
        if kind == "ping":
            self._ack(client, req_id, True)
        elif kind == "calibration.watch":
            compare = getattr(self.rt.camera_service, "compare", None)
            if compare is None or not callable(compare):
                self._ack(client, req_id, False, "not_found",
                          "Calibration is not available on this backend.")
                return
            client.watch_ref = str(message.get("ref_id") or "active")
            self._last_watch_at = time.monotonic()
            self._ack(client, req_id, True)
            self._ensure_loop("drift", self._drift_loop)
        elif kind == "calibration.unwatch":
            client.watch_ref = None
            self._ack(client, req_id, True)
        # Unknown message types are ignored (forward compatibility).

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        client = _Client(websocket)
        self._clients.add(client)
        client.writer = asyncio.create_task(self._writer(client), name="ws-ui-writer")
        try:
            snapshot = build_snapshot(self.rt)
            client.offer({"type": "snapshot", "data": snapshot.model_dump(mode="json")})
        except Exception:
            logger.exception("initial snapshot failed")
        self._ensure_loop("snapshot", self._snapshot_loop)
        self._ensure_loop("live", self._live_loop)
        try:
            while True:
                raw = await websocket.receive_text()
                await self._handle_client_message(client, raw)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug("ws/ui client error: %s", exc)
        finally:
            self._clients.discard(client)
            if client.writer is not None:
                client.writer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await client.writer


@router.websocket("/ws/ui")
async def ui_ws(websocket: WebSocket) -> None:
    from .runtime import get_runtime

    hub = getattr(get_runtime(), "hub", None)
    if hub is None:
        await websocket.close(code=1011)
        return
    await hub.serve(websocket)