"""Session finalization as a resumable, observable job (restructure plan, piece 03).

WHY this exists
---------------
Finalization used to run inline inside the WebSocket message handler that received
STOP: close every writer, re-sort every CSV, hash it, validate every row, write the
ledger — all before the ACK was sent. That had four consequences, every one of which
showed up in the field:

  * the dashboard was blind for the whole span (no transition was ever broadcast), so a
    slow-but-healthy finalize was indistinguishable from a hang;
  * any exception escaped into the socket handler and closed the dashboard connection
    instead of failing the command;
  * the ACK could not arrive for minutes, so the client timeout was inflated to 120 s and
    its retry then re-entered STOP and produced an empty report;
  * a backend that died mid-finalize could not resume — startup ran a *second*,
    divergent reimplementation of the same work.

Here finalization is an ordered list of idempotent steps. Each one records its state
before and after, reports progress to observers, and isolates its own failure so one bad
artifact cannot stop the rest. Because step state is durable, a restart resumes from the
first step that did not complete instead of redoing everything down a different path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .audit_logger import audit
from .io_manager import io_manager

logger = logging.getLogger(__name__)

# Building the server-side archive at STOP means the operator always has one complete,
# checksummed file on the SSD even if every browser-side export path fails. Set
# FINALIZE_AUTO_BUNDLE=false to skip it on a disk-constrained rig.
_AUTO_BUNDLE = os.getenv("FINALIZE_AUTO_BUNDLE", "true").lower() != "false"

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class FinalizeContext:
    """Everything a finalize run needs, plus what it produces as it goes."""

    session_id: str
    reason: str = "operator_stop"
    scheduled_start_ms: int = 0
    session_start_ms: int = 0
    session_end_ms: int = 0
    devices: list = field(default_factory=list)
    label_timeline: list = field(default_factory=list)
    true_hz: dict = field(default_factory=dict)

    # Produced by the steps.
    file_results: dict = field(default_factory=dict)
    report: dict | None = None
    bundle: dict | None = None

    # Injected by the caller so this module never imports session_manager (which
    # imports this one).
    collect_artifacts: Callable[[], Awaitable[tuple[dict, list]]] | None = None
    persist: Callable[["FinalizeJob"], Awaitable[None]] | None = None
    notify: Callable[[dict], Awaitable[None]] | None = None
    enter_state: Callable[[str], Awaitable[None]] | None = None


class FinalizeJob:
    """One finalization run. Safe to re-run: completed steps are skipped."""

    def __init__(self, ctx: FinalizeContext, steps: dict[str, dict] | None = None) -> None:
        self.ctx = ctx
        self.session_id = ctx.session_id
        self.started_ms = int(time.time() * 1000)
        self.finished_ms = 0
        # name -> {state, detail, started_ms, ended_ms}
        self.steps: dict[str, dict] = dict(steps or {})
        for name, _, _ in self.STEPS:
            self.steps.setdefault(name, {"state": PENDING, "detail": ""})

    # ── Step implementations ────────────────────────────────────────────────

    async def _step_close_writers(self) -> None:
        """Flush, fsync, re-sort and digest every open CSV.

        Unifies the two finalize paths. When the writers for this session are still open
        (the ordinary STOP) they are closed here. When they are not — a backend that died
        mid-session and is now finalizing at startup — the same step inventories the
        artifacts from disk instead, so both cases produce one `file_results` shape and
        run through exactly the same validation, report and bundle steps below.
        """
        if io_manager.session_id == self.session_id:
            self.ctx.file_results = await io_manager.close_session(self.ctx.true_hz)
            return
        if self.ctx.collect_artifacts is None:
            self.ctx.file_results = {}
            return
        file_results, devices = await self.ctx.collect_artifacts()
        self.ctx.file_results = file_results
        # Devices reconstructed from the ledger — the live registry is empty after a restart.
        if devices:
            self.ctx.devices = devices

    async def _step_validate(self) -> None:
        from .integrity_validator import IntegrityValidator

        self.ctx.report = await IntegrityValidator().run(
            session_id=self.session_id,
            file_results=self.ctx.file_results,
            devices=list(self.ctx.devices),
            scheduled_start_ms=self.ctx.scheduled_start_ms,
            label_timeline=self.ctx.label_timeline,
            session_start_ms=self.ctx.session_start_ms,
            session_end_ms=self.ctx.session_end_ms or int(time.time() * 1000),
        )

    async def _step_bundle(self) -> None:
        """Write the session's data artifacts into one archive on the SSD.

        This is the durability backstop: the browser export can fail, be cancelled, or
        run on a machine whose File System Access handle dies, and the operator still has
        a complete checksummed zip next to the CSVs.
        """
        if not _AUTO_BUNDLE:
            self.steps["bundle"]["detail"] = "disabled by FINALIZE_AUTO_BUNDLE"
            raise _Skip()
        if not self.ctx.file_results:
            self.steps["bundle"]["detail"] = "no artifacts to bundle"
            raise _Skip()
        from .export import build_session_bundle

        self.ctx.bundle = await build_session_bundle(self.session_id)

    # (step name, method, session state to enter before running it). The state column
    # keeps the documented machine honest: VALIDATING is a real, broadcast state rather
    # than a value that only ever existed in the enum.
    STEPS: list[tuple[str, str, str | None]] = [
        ("close_writers", "_step_close_writers", None),
        ("validate", "_step_validate", "VALIDATING"),
        ("bundle", "_step_bundle", None),
    ]

    # ── Runner ──────────────────────────────────────────────────────────────

    @property
    def is_complete(self) -> bool:
        return all(
            s["state"] in (DONE, FAILED, SKIPPED) for s in self.steps.values()
        )

    @property
    def failed_steps(self) -> list[str]:
        return [n for n, s in self.steps.items() if s["state"] == FAILED]

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
            "steps": [
                {"step": name, **self.steps[name]} for name, _, _ in self.STEPS
            ],
            "failed": self.failed_steps,
        }

    async def _emit(self) -> None:
        if self.ctx.notify is None:
            return
        try:
            await self.ctx.notify({"type": "FINALIZE_PROGRESS", **self.snapshot()})
        except Exception as exc:                      # observers must never break the job
            logger.debug("finalize observer failed: %s", exc)

    async def _checkpoint(self) -> None:
        if self.ctx.persist is None:
            return
        try:
            await self.ctx.persist(self)
        except Exception as exc:
            # A failure to record progress must not abort work that is succeeding. The
            # artifacts on disk remain authoritative either way.
            logger.warning("finalize checkpoint failed: %s", exc)

    async def run(self) -> dict:
        """Run every step that has not already completed. Never raises."""
        for name, attr, state in self.STEPS:
            if self.steps[name]["state"] in (DONE, SKIPPED):
                continue
            if state is not None and self.ctx.enter_state is not None:
                try:
                    await self.ctx.enter_state(state)
                except Exception as exc:
                    logger.debug("enter_state(%s) failed: %s", state, exc)
            self.steps[name].update(
                {"state": RUNNING, "started_ms": int(time.time() * 1000), "detail": ""}
            )
            await self._checkpoint()
            await self._emit()
            try:
                await getattr(self, attr)()
                self.steps[name]["state"] = DONE
            except _Skip:
                self.steps[name]["state"] = SKIPPED
            except asyncio.CancelledError:
                self.steps[name].update({"state": FAILED, "detail": "cancelled"})
                await self._checkpoint()
                raise
            except Exception as exc:
                # Isolate: a failed close, a failed validation or a failed bundle must
                # each leave the others runnable and still produce a terminal record.
                self.steps[name].update({"state": FAILED, "detail": str(exc)})
                logger.error("finalize step %s failed: %s", name, exc, exc_info=True)
                await audit.log(
                    "ERROR",
                    "finalize_step_failed",
                    {"session_id": self.session_id, "step": name, "error": str(exc)},
                )
            finally:
                self.steps[name]["ended_ms"] = int(time.time() * 1000)
            await self._checkpoint()
            await self._emit()

        self.finished_ms = int(time.time() * 1000)
        await audit.log(
            "INFO",
            "finalize_complete",
            {
                "session_id": self.session_id,
                "duration_ms": self.finished_ms - self.started_ms,
                "failed_steps": self.failed_steps,
            },
        )
        return self.ctx.report or {}


class _Skip(Exception):
    """Raised by a step that deliberately did no work."""
