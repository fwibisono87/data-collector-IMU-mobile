"""
Async CSV writer with SSD fallback (CLAUDE.md §9.4).
One file handle per device per session. fsync every FSYNC_INTERVAL_SEC seconds.
"""
import asyncio
import bisect
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import aiofiles

from .audit_logger import audit
from .dedup_store import dedup
from .csv_schema import (
    CSV_HEADER as _CSV_HEADER,
    metadata_line,
    strip_tier_token,
)
from master_backend.proto.sensor_packet import SensorPacket

logger = logging.getLogger(__name__)

_FSYNC_INTERVAL = int(os.getenv("FSYNC_INTERVAL_SEC", "5"))
_LATE_ACCEPT_SEC = int(os.getenv("LATE_ACCEPT_SEC", "600"))
_SORT_ON_CLOSE = os.getenv("SORT_CSV_ON_CLOSE", "true").lower() == "true"
_SORT_REPLACE_ATTEMPTS = max(1, int(os.getenv("SORT_REPLACE_ATTEMPTS", "5")))
_SORT_REPLACE_BACKOFF_SEC = float(os.getenv("SORT_REPLACE_BACKOFF_SEC", "0.2"))
_DEFAULT_LABEL_ID = 0
_DEFAULT_LABEL_NAME = "0"


class DeviceWriter:
    """Manages one open CSV file for one device."""

    def __init__(self, path: Path, metadata_line: str) -> None:
        self._path = path
        self._metadata_line = metadata_line
        self._file = None
        self._rows_written = 0
        self._last_fsync = time.monotonic()

    async def open(self, *, append_if_exists: bool = False) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        exists = append_if_exists and self._path.exists() and self._path.stat().st_size > 0
        mode = "a" if exists else "w"
        self._file = await aiofiles.open(self._path, mode=mode, encoding="utf-8", newline="")
        if not exists:
            await self._file.write(self._metadata_line + "\n")
            await self._file.write(_CSV_HEADER)

    async def write_row(self, row: str) -> None:
        if self._file is None:
            return
        await self._file.write(row)
        self._rows_written += 1
        now = time.monotonic()
        if now - self._last_fsync >= _FSYNC_INTERVAL:
            await self._file.flush()
            await asyncio.get_event_loop().run_in_executor(
                None, os.fsync, self._file.fileno()
            )
            self._last_fsync = now

    async def close(self, *, sort: bool = True) -> dict:
        """Flush, fsync and close. `sort=False` skips the re-order pass.

        Retiring a writer left over from a previous session must still make its bytes
        durable, but must not spend an unbounded re-sort inside START — the merge that
        consolidates that session re-sorts anyway.
        """
        if self._file:
            await self._file.flush()
            await asyncio.get_event_loop().run_in_executor(
                None, os.fsync, self._file.fileno()
            )
            await self._file.close()
            self._file = None
        if _SORT_ON_CLOSE and sort:
            reorder = await asyncio.get_event_loop().run_in_executor(
                None, _sort_rows_by_timestamp, self._path
            )
        else:
            reorder = {"reordered": 0}
        sha256 = _sha256(self._path)
        return {
            "path": str(self._path),
            "rows": self._rows_written,
            "sha256": sha256,
            **reorder,
        }

    async def abandon(self) -> None:
        """Release a failed file handle without another flush/fsync attempt.

        A write error commonly means the mounted SSD is no longer usable.  Calling the
        normal close path would retry the failing fsync and can prevent the in-memory
        writer from switching to the rescue filesystem.
        """
        if self._file is None:
            return
        try:
            await self._file.close()
        except OSError:
            pass
        finally:
            self._file = None


def _format_row(pkt: SensorPacket, label_id: int, label_name: str) -> str:
    acc_ts = pkt.acc_ts_ms if pkt.acc_ts_ms else ""
    gyro_ts = pkt.gyro_ts_ms if pkt.gyro_ts_ms else ""
    # A pre-v2 phone sends no sample_kind, and the parser defaults it to 0 — which would
    # assert "this IS a fresh hardware reading" about a packet whose provenance we do not
    # know. Write empty instead: unknown, not fresh. This matters during a staged rollout,
    # when some phones in a session are still on v1.
    sample_kind = pkt.sample_kind if pkt.schema_version >= 2 else ""
    return (
        f"{pkt.timestamp_ms},"
        f"{pkt.acc_x:.6f},{pkt.acc_y:.6f},{pkt.acc_z:.6f},"
        f"{pkt.gyro_x:.6f},{pkt.gyro_y:.6f},{pkt.gyro_z:.6f},"
        f"{label_id},{label_name},"
        f"{pkt.sequence_number},{pkt.device_id},"
        f"{acc_ts},{gyro_ts},{sample_kind}\n"
    )


def _sort_rows_by_timestamp(path: Path) -> dict:
    """Restore monotonic time order.

    A mid-session reconnect replays the phone's buffered packets while the live 100 Hz
    stream continues, so old rows land after new ones and timestamp_ms goes backwards by
    minutes (plan D9). The downstream segmentation pipeline assumes a monotonic series.
    Rewrites only when the file is actually out of order; the metadata line and CSV header
    are preserved verbatim.
    """
    if not path.exists():
        return {"reordered": 0}

    # Do the detection and reorder on disk.  The old splitlines()+sort path created a
    # second Python string for every row at STOP, which could take hundreds of MB for a
    # long three-device session — exactly when the operator needs finalisation to be the
    # most reliable operation.
    db_path = path.with_name(path.name + ".sort.sqlite")
    tmp = path.with_name(path.name + ".sort.tmp")
    out_of_order = 0
    previous_ts: int | None = None
    head: list[str] = []
    replacement_succeeded = False
    try:
        try:
            db_path.unlink()
        except OSError:
            pass
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rows (arrival INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as source:
                head = [next(source, ""), next(source, "")]
                for line in source:
                    try:
                        timestamp = int(line.split(",", 1)[0])
                    except (TypeError, ValueError, IndexError):
                        timestamp = -1
                    if previous_ts is not None and timestamp < previous_ts:
                        out_of_order += 1
                    previous_ts = timestamp
                    conn.execute(
                        "INSERT INTO rows(timestamp, payload) VALUES (?, ?)",
                        (timestamp, line),
                    )
                    if out_of_order and out_of_order % 2000 == 0:
                        conn.commit()
            conn.commit()
            if out_of_order == 0:
                return {"reordered": 0}

            # Never truncate the only good CSV in place. A process death during the rewrite
            # leaves the original intact; a failed replacement leaves this temporary output
            # available for recovery instead of deleting it in the finalizer.
            with open(tmp, "w", encoding="utf-8", newline="") as out:
                out.writelines(head)
                for (payload,) in conn.execute(
                    "SELECT payload FROM rows ORDER BY timestamp, arrival"
                ):
                    out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            _replace_sorted_csv_with_retry(tmp, path)
            replacement_succeeded = True
            return {"reordered": out_of_order}
        finally:
            conn.close()
    finally:
        try:
            db_path.unlink()
        except OSError:
            pass
        # If replacement failed, retain the fully-written sorted output as a recovery
        # artifact. A transient Windows AV/indexer lock should not destroy the only copy
        # that could be installed after the session is closed.
        if replacement_succeeded:
            try:
                tmp.unlink()
            except OSError:
                pass


def _replace_sorted_csv_with_retry(source: Path, target: Path) -> None:
    """Atomically install a sorted CSV, tolerating short-lived Windows file locks.

    Windows can briefly deny ``os.replace`` while an AV/indexing process has opened the
    just-written temporary file. Retrying only the replace keeps the original CSV intact
    and avoids masking persistent failures as successful finalization.
    """
    last_error: OSError | None = None
    for attempt in range(_SORT_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            # WinError 5 (access denied) and 32 (sharing violation) are the transient
            # lock cases. Other errors indicate a real path/filesystem problem and should
            # fail immediately.
            winerror = getattr(exc, "winerror", None)
            if not isinstance(exc, PermissionError) and winerror not in (5, 32):
                raise
            last_error = exc
            if attempt + 1 < _SORT_REPLACE_ATTEMPTS:
                time.sleep(_SORT_REPLACE_BACKOFF_SEC * (2 ** attempt))
    assert last_error is not None
    raise last_error


class IoManager:
    def __init__(self) -> None:
        self._writers: dict[str, DeviceWriter] = {}
        self._rescue_writers: dict[str, DeviceWriter] = {}
        self._active_label_id: int = _DEFAULT_LABEL_ID
        self._active_label_name: str = _DEFAULT_LABEL_NAME
        self._session_id: str = ""
        self._ssd_path: Path = Path(os.getenv("SSD_PATH", "./data"))
        self._rescue_path: Path = Path(os.getenv("RESCUE_PATH", "./data_rescue"))

        self._base: Path | None = None
        self._metadata_line: str = ""
        self._session_open: bool = False
        self._dropped_no_writer: dict[str, int] = {}
        # A failed primary writer is retried once on RESCUE_PATH.  These counters make any
        # row that could not be rescued visible in the final integrity report.
        self._write_failures: dict[str, int] = {}
        self._rows_lost_after_failover: dict[str, int] = {}

        # Label timeline: applied_at_ms ascending, parallel value list (plan D14 / T16).
        self._label_ts: list[int] = []
        self._label_val: list[tuple[int, str]] = []

        # Late-delivery window (plan DD-4)
        self._late_session_id: str = ""
        self._late_base: Path | None = None
        self._late_metadata: str = ""
        self._late_closed_at: float = 0.0
        self._late_writers: dict[str, DeviceWriter] = {}
        self._late_rows: dict[str, int] = {}

    def set_label(self, label_id: int, label_name: str) -> None:
        self._active_label_id = label_id
        self._active_label_name = label_name
        now = int(time.time() * 1000)
        # Monotonic guard: two labels in the same millisecond → last one wins.
        if self._label_ts and self._label_ts[-1] == now:
            self._label_val[-1] = (label_id, label_name)
        else:
            self._label_ts.append(now)
            self._label_val.append((label_id, label_name))

    def label_at(self, ts_ms: int) -> tuple[int, str]:
        """Label that was active when this packet was SAMPLED.

        Rows replayed from a phone's offline buffer arrive minutes after they were taken;
        stamping them with the label active at WRITE time silently mislabelled the entire
        buffered segment (plan D14). Packet timestamps are clock-offset corrected
        (ClockSyncService), so they are directly comparable to the backend wall clock —
        which is exactly what the clock-sync handshake exists for.
        """
        if not self._label_ts:
            return _DEFAULT_LABEL_ID, _DEFAULT_LABEL_NAME
        i = bisect.bisect_right(self._label_ts, ts_ms) - 1
        if i < 0:
            return _DEFAULT_LABEL_ID, _DEFAULT_LABEL_NAME
        return self._label_val[i]

    @property
    def late_session_id(self) -> str:
        """Session still accepting late telemetry, or '' — advertised to phones on PONG."""
        if not self._late_session_id:
            return ""
        if time.monotonic() - self._late_closed_at > _LATE_ACCEPT_SEC:
            return ""
        return self._late_session_id

    @property
    def session_id(self) -> str:
        """Session currently owned by the writer/late-delivery pipeline."""
        return self._session_id

    @property
    def session_open(self) -> bool:
        """True while writers for a session are open (i.e. close_session is still owed)."""
        return self._session_open or bool(self._writers) or bool(self._rescue_writers)

    @property
    def label_timeline(self) -> list[dict]:
        """JSON-safe label transitions for the current/just-ended session."""
        return [
            {
                "timestamp_ms": timestamp,
                "label_id": value[0],
                "label_name": value[1],
            }
            for timestamp, value in zip(self._label_ts, self._label_val)
        ]

    def has_writer(self, device_id: str) -> bool:
        return device_id in self._writers or device_id in self._rescue_writers

    def dropped_no_writer(self, device_id: str) -> int:
        return self._dropped_no_writer.get(device_id, 0)

    def write_failures(self, device_id: str) -> int:
        return self._write_failures.get(device_id, 0)

    def rows_lost_after_failover(self, device_id: str) -> int:
        return self._rows_lost_after_failover.get(device_id, 0)

    def _rescue_path_for(self, writer: DeviceWriter) -> Path:
        assert self._base is not None
        return (
            self._rescue_path / "Data_Riset_IMU" / self._base.name
            / writer._path.name.replace(".csv", "_rescue.csv")
        )

    async def _activate_rescue_writer(
        self, device_id: str, failed_writer: DeviceWriter
    ) -> DeviceWriter | None:
        """Create the runtime fallback writer after a primary SSD write fails."""
        existing = self._rescue_writers.get(device_id)
        if existing is not None:
            return existing
        rescue_path = self._rescue_path_for(failed_writer)
        rescue_writer = DeviceWriter(rescue_path, self._metadata_line)
        try:
            await rescue_writer.open(append_if_exists=True)
        except OSError as exc:
            await audit.log("ERROR", "rescue_path_open_failed", {
                "device_id": device_id, "path": str(rescue_path), "error": str(exc),
            })
            return None
        self._rescue_writers[device_id] = rescue_writer
        await audit.log("WARN", "rescue_path_activated", {
            "device_id": device_id, "path": str(rescue_path), "reason": "runtime_write_failure",
        })
        return rescue_writer

    async def _open_writer_for(self, device_id: str, role: str, true_hz: float = 0.0) -> None:
        # Deliberately NO sampling-rate token in the name. The tier was derived from
        # true_sensor_hz (distinct hardware readings) while rows are emitted by a separate
        # ~100 Hz timer, so the label described neither the row cadence nor a uniform grid:
        # session 1786677865027 wrote `chest_75hz` for a file whose 56,173 rows span 575.9 s
        # at 97.5 rows/s. Reconstructing time as rows/label was wrong by up to 30%, which is
        # exactly how an analyst is most likely to read a file called `_75hz_`.
        #
        # These files are irregularly sampled and timestamp_ms is the only correct axis.
        # The measured rates now live in <session>_<role>_timing.json instead, where they
        # can be stated precisely rather than rounded into a misleading tier.
        fname = f"{self._session_id}_{role}_sensor_data.csv"
        path = self._base / fname
        writer = DeviceWriter(path, self._metadata_line)
        try:
            await writer.open(append_if_exists=True)
            self._writers[device_id] = writer
            await audit.log("INFO", "csv_opened", {"path": str(path), "device_id": device_id})
        except OSError as exc:
            await audit.log("ERROR", "ssd_write_failed", {"error": str(exc), "device_id": device_id})
            rescue_writer = await self._activate_rescue_writer(device_id, writer)
            if rescue_writer is None:
                await audit.log("ERROR", "no_rescue_writer", {"device_id": device_id})

    async def ensure_writer(self, device_id: str, role: str, true_hz: float = 0.0) -> bool:
        """Open a CSV for a device that joined (or rejoined) AFTER the session started.

        Without this, every packet from such a device was discarded by write_packet's
        `if writer is None: return` — with the dashboard still counting packets, so the
        operator saw a healthy green device and got no file at all (plan D2).
        """
        if not self._session_open or self.has_writer(device_id):
            return self.has_writer(device_id)
        await self._open_writer_for(device_id, role, true_hz)
        await audit.log("WARN", "late_writer_created",
                        {"device_id": device_id, "role": role, "session_id": self._session_id})
        return self.has_writer(device_id)

    async def open_session(
        self,
        session_id: str,
        subject_name: str,
        session_tag: str,
        operator: str,
        device_roles: dict[str, str],  # device_id -> role
        device_rates: dict[str, float] | None = None,  # device_id -> preflight true_hz
        preflight_failed: list[str] | None = None,     # preflight checks red at START
    ) -> None:
        # A new session starting while a previous late-delivery window is still open must
        # not leak its file handles or silently drop the pending summary (plan R7).
        # Guarded: a failure to tidy the *previous* session must never prevent the next
        # one from starting. Before this, one failed late-summary write left the window
        # permanently armed and every subsequent START raised here.
        try:
            await self.finalize_late(force=True)
        except Exception as exc:
            await audit.log("ERROR", "late_finalize_before_open_failed", {
                "session_id": session_id, "error": str(exc),
            })
            self._reset_late_state()

        # Retire any writer still held from a previous session. close_session clears
        # these, but a session that ended by any other route (abort, crash recovery,
        # a failed close) left them in place — the old handle was then silently replaced
        # and never flushed, and a stale rescue writer was closed at the *next* stop,
        # putting the previous session's path and row count into this session's results.
        await self._retire_writers()
        # Bitsets are keyed by (device, session) so they cannot collide across sessions,
        # but without this they would accumulate for the life of the process on a rig that
        # runs many sessions between restarts.
        dedup.clear()

        self._session_id = session_id
        folder_name = f"{subject_name}_{session_tag}".replace(" ", "_")
        self._base = self._ssd_path / "Data_Riset_IMU" / folder_name
        # A session started over a red preflight records that fact in its own data file, so an
        # analyst reading the CSV months later sees it without needing the audit log.
        self._metadata_line = metadata_line(
            session_id=session_id, subject=subject_name, operator=operator,
            extra={"preflight_failed": ";".join(preflight_failed)} if preflight_failed else None,
        )
        self._session_open = True
        self._dropped_no_writer.clear()
        self._write_failures.clear()
        self._rows_lost_after_failover.clear()

        self._label_ts = [int(time.time() * 1000)]
        self._label_val = [(_DEFAULT_LABEL_ID, _DEFAULT_LABEL_NAME)]

        rates = device_rates or {}
        for device_id, role in device_roles.items():
            await self._open_writer_for(device_id, role, rates.get(device_id, 0.0))

    async def _retire_writers(self) -> None:
        """Release every open writer without claiming its output is a finalized artifact."""
        stale = {**self._writers, **self._rescue_writers}
        for device_id, writer in stale.items():
            try:
                await writer.close(sort=False)
            except Exception as exc:
                await audit.log("ERROR", "stale_writer_close_failed", {
                    "device_id": device_id, "path": str(writer._path), "error": str(exc),
                })
                await writer.abandon()
        if stale:
            await audit.log("WARN", "stale_writers_retired", {
                "count": len(stale), "session_id": self._session_id,
            })
        self._writers.clear()
        self._rescue_writers.clear()

    def _reset_late_state(self) -> None:
        """Drop late-window bookkeeping after a failed finalize, so it cannot re-arm."""
        self._late_writers.clear()
        self._late_rows.clear()
        self._late_session_id = ""
        self._late_base = None

    async def write_packet(self, pkt: SensorPacket) -> None:
        primary_writer = self._writers.get(pkt.device_id)
        writer = primary_writer or self._rescue_writers.get(pkt.device_id)
        if writer is None:
            self._dropped_no_writer[pkt.device_id] = self._dropped_no_writer.get(pkt.device_id, 0) + 1
            n = self._dropped_no_writer[pkt.device_id]
            if n == 1 or n % 1000 == 0:      # log the first, then every 1000
                await audit.log("ERROR", "packet_dropped_no_writer",
                                {"device_id": pkt.device_id, "count": n})
            return

        row = _format_row(pkt, *self.label_at(pkt.timestamp_ms))
        try:
            await writer.write_row(row)
        except OSError as exc:
            device_id = pkt.device_id
            self._write_failures[device_id] = self._write_failures.get(device_id, 0) + 1
            await audit.log("ERROR", "csv_write_error", {
                "error": str(exc), "device_id": device_id,
                "writer": "primary" if writer is primary_writer else "rescue",
            })

            # A primary SSD can disappear long after a session starts. Retire its handle,
            # create a rescue writer on demand, and retry this exact row once.
            if writer is primary_writer:
                self._writers.pop(device_id, None)
                await writer.abandon()
                writer = await self._activate_rescue_writer(device_id, writer)
            else:
                writer = None

            if writer is not None:
                try:
                    await writer.write_row(row)
                    return
                except OSError as retry_exc:
                    self._write_failures[device_id] = self._write_failures.get(device_id, 0) + 1
                    await audit.log("ERROR", "rescue_write_failed", {
                        "error": str(retry_exc), "device_id": device_id,
                    })

            self._rows_lost_after_failover[device_id] = (
                self._rows_lost_after_failover.get(device_id, 0) + 1
            )
            await audit.log("ERROR", "packet_lost_after_failover", {
                "device_id": device_id,
                "count": self._rows_lost_after_failover[device_id],
            })

    async def write_late(self, pkt: SensorPacket, role: str) -> None:
        """Append a post-STOP packet to <session>_<role>_sensor_data_late.csv."""
        if not self.late_session_id or self._late_base is None:
            return
        writer = self._late_writers.get(pkt.device_id)
        if writer is None:
            path = (self._late_base
                    / f"{self._late_session_id}_{role}_sensor_data_late.csv")
            writer = DeviceWriter(path, self._late_metadata + ",late_delivery=1")
            await writer.open(append_if_exists=True)
            self._late_writers[pkt.device_id] = writer
            await audit.log("WARN", "late_delivery_started",
                            {"device_id": pkt.device_id, "role": role, "path": str(path),
                             "session_id": self._late_session_id})
        await writer.write_row(_format_row(pkt, *self.label_at(pkt.timestamp_ms)))
        self._late_rows[pkt.device_id] = self._late_rows.get(pkt.device_id, 0) + 1

    def arm_late_window(self) -> None:
        """Make the just-ending session eligible for post-STOP packets immediately.

        This is deliberately separate from close_session(): STOP control messages may cause a
        phone to flush while the main writers are still being fsynced and sorted.
        """
        if self._late_session_id == self._session_id and self._late_base == self._base:
            return
        self._late_session_id = self._session_id
        self._late_base = self._base
        self._late_metadata = self._metadata_line
        self._late_closed_at = time.monotonic()
        self._late_rows = {}

    async def close_session(self, device_rates: dict[str, float] | None = None) -> dict:
        """Close every writer.

        `device_rates` is the session-wide average of DISTINCT readings per second per
        device. Filenames no longer encode a rate, so nothing is renamed here; the rate is
        reported in the per-role timing sidecar instead. The parameter is kept because
        callers pass it and it still reaches the audit log.
        """
        results = {}
        for device_id, writer in {**self._writers, **self._rescue_writers}.items():
            try:
                results[device_id] = await writer.close()
            except Exception as exc:
                # One failing handle must not prevent every other device from being
                # closed and included in the terminal bundle. Preserve the path and row
                # count we know, then let integrity mark this artifact as failed.
                await audit.log("ERROR", "csv_close_failed", {
                    "device_id": device_id,
                    "path": str(writer._path),
                    "error": str(exc),
                })
                await writer.abandon()
                try:
                    sha = _sha256(writer._path)
                except OSError:
                    sha = ""
                sort_tmp = writer._path.with_name(writer._path.name + ".sort.tmp")
                results[device_id] = {
                    "path": str(writer._path),
                    "rows": writer._rows_written,
                    "sha256": sha,
                    "close_failed": True,
                    "close_error": str(exc),
                    "reordered": 0,
                }
                if sort_tmp.exists():
                    results[device_id]["sort_recovery_path"] = str(sort_tmp)

        # No rename pass. Files used to be reopened under a corrected `_<n>hz_` token here,
        # which could fail and leave a stale label on correct data; now the name never
        # claimed a rate to begin with. The measured average is recorded for the audit
        # trail and surfaced properly by the timing sidecar.
        rates = device_rates or {}
        for device_id, result in results.items():
            result["session_avg_hz"] = round(rates.get(device_id, 0.0), 2)

        # Arm the late-delivery window: a phone that reconnects within LATE_ACCEPT_SEC of
        # STOP still gets its buffered tail written, to a sidecar (plan DD-4).
        self.arm_late_window()

        self._session_open = False
        self._writers.clear()
        self._rescue_writers.clear()
        self._active_label_id = _DEFAULT_LABEL_ID
        self._active_label_name = _DEFAULT_LABEL_NAME
        # Do NOT clear _label_ts/_label_val here — write_late needs the timeline for the
        # whole late window (plan T16). Cleared only in open_session, for the next session.
        return results

    async def finalize_late(self, force: bool = False) -> dict | None:
        """Close late writers and write <session>_late_delivery.json.

        Called two ways: by the idle reaper once LATE_ACCEPT_SEC has elapsed
        (force=False, expiry-driven), and by open_session (force=True) so a new session
        starting while a late window is still open doesn't leak file handles or silently
        drop the pending summary (plan R7).
        """
        if not self._late_session_id:
            return None
        if not force and time.monotonic() - self._late_closed_at <= _LATE_ACCEPT_SEC:
            return None
        summary = {"session_id": self._late_session_id, "devices": {}}
        for device_id, w in self._late_writers.items():
            try:
                closed = await w.close()
            except Exception as exc:
                await audit.log("ERROR", "late_csv_close_failed", {
                    "device_id": device_id, "error": str(exc),
                })
                await w.abandon()
                closed = {
                    "path": str(w._path), "rows": w._rows_written, "sha256": "",
                    "close_failed": True, "close_error": str(exc), "reordered": 0,
                }
            summary["devices"][device_id] = {
                **closed,
                "rows_appended": self._late_rows.get(device_id, 0),
            }
        try:
            if summary["devices"] and self._late_base is not None:
                (self._late_base / f"{self._late_session_id}_late_delivery.json").write_text(
                    json.dumps(summary, indent=2)
                )
                await audit.log("WARN", "late_delivery_finalized", summary)
        except OSError as exc:
            # The sidecar CSVs are already closed and fsynced above; only the summary
            # failed. Record that and still tear the window down — leaving it armed used
            # to make every subsequent session start raise here forever.
            await audit.log("ERROR", "late_summary_write_failed", {
                "session_id": self._late_session_id, "error": str(exc),
            })
        finally:
            self._reset_late_state()
            # The dedup bitsets are the memory that stops an already-written packet being
            # re-accepted. They belong to the delivery window, not to the stop: clearing
            # them at STOP (while this window stayed open for LATE_ACCEPT_SEC) let a
            # reconnecting phone re-deliver rows that were already on disk.
            dedup.clear()
        return summary if summary["devices"] else None


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


io_manager = IoManager()
