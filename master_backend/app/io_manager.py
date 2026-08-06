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
import time
from pathlib import Path

import aiofiles

from .audit_logger import audit
from .csv_schema import CSV_HEADER as _CSV_HEADER, metadata_line
from master_backend.proto.sensor_packet import SensorPacket

logger = logging.getLogger(__name__)

_FSYNC_INTERVAL = int(os.getenv("FSYNC_INTERVAL_SEC", "5"))
_LATE_ACCEPT_SEC = int(os.getenv("LATE_ACCEPT_SEC", "600"))
_SORT_ON_CLOSE = os.getenv("SORT_CSV_ON_CLOSE", "true").lower() == "true"
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

    async def close(self) -> dict:
        if self._file:
            await self._file.flush()
            await asyncio.get_event_loop().run_in_executor(
                None, os.fsync, self._file.fileno()
            )
            await self._file.close()
            self._file = None
        if _SORT_ON_CLOSE:
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


def _format_row(pkt: SensorPacket, label_id: int, label_name: str) -> str:
    acc_ts = pkt.acc_ts_ms if pkt.acc_ts_ms else ""
    gyro_ts = pkt.gyro_ts_ms if pkt.gyro_ts_ms else ""
    return (
        f"{pkt.timestamp_ms},"
        f"{pkt.acc_x:.6f},{pkt.acc_y:.6f},{pkt.acc_z:.6f},"
        f"{pkt.gyro_x:.6f},{pkt.gyro_y:.6f},{pkt.gyro_z:.6f},"
        f"{label_id},{label_name},"
        f"{pkt.sequence_number},{pkt.device_id},"
        f"{acc_ts},{gyro_ts},{pkt.sample_kind}\n"
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
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) <= 2:
        return {"reordered": 0}
    head, body = lines[:2], lines[2:]

    def ts(line: str) -> int:
        try:
            return int(line.split(",", 1)[0])
        except Exception:
            return -1

    out_of_order = sum(1 for a, b in zip(body, body[1:]) if ts(a) > ts(b))
    if out_of_order == 0:
        return {"reordered": 0}
    body.sort(key=ts)                     # Python's sort is stable → ties keep arrival order
    path.write_text("".join(head + body), encoding="utf-8")
    return {"reordered": out_of_order}


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

    def has_writer(self, device_id: str) -> bool:
        return device_id in self._writers or device_id in self._rescue_writers

    def dropped_no_writer(self, device_id: str) -> int:
        return self._dropped_no_writer.get(device_id, 0)

    async def _open_writer_for(self, device_id: str, role: str) -> None:
        fname = f"{self._session_id}_{role}_sensor_data.csv"
        path = self._base / fname
        writer = DeviceWriter(path, self._metadata_line)
        try:
            await writer.open(append_if_exists=True)
            self._writers[device_id] = writer
            await audit.log("INFO", "csv_opened", {"path": str(path), "device_id": device_id})
        except OSError as exc:
            await audit.log("ERROR", "ssd_write_failed", {"error": str(exc), "device_id": device_id})
            rescue_path = (
                self._rescue_path / "Data_Riset_IMU" / self._base.name
                / fname.replace(".csv", "_rescue.csv")
            )
            rescue_writer = DeviceWriter(rescue_path, self._metadata_line)
            await rescue_writer.open(append_if_exists=True)
            self._rescue_writers[device_id] = rescue_writer
            await audit.log("INFO", "rescue_path_activated", {"path": str(rescue_path)})

    async def ensure_writer(self, device_id: str, role: str) -> bool:
        """Open a CSV for a device that joined (or rejoined) AFTER the session started.

        Without this, every packet from such a device was discarded by write_packet's
        `if writer is None: return` — with the dashboard still counting packets, so the
        operator saw a healthy green device and got no file at all (plan D2).
        """
        if not self._session_open or self.has_writer(device_id):
            return self.has_writer(device_id)
        await self._open_writer_for(device_id, role)
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
    ) -> None:
        # A new session starting while a previous late-delivery window is still open must
        # not leak its file handles or silently drop the pending summary (plan R7).
        await self.finalize_late(force=True)

        self._session_id = session_id
        folder_name = f"{subject_name}_{session_tag}".replace(" ", "_")
        self._base = self._ssd_path / "Data_Riset_IMU" / folder_name
        self._metadata_line = metadata_line(
            session_id=session_id, subject=subject_name, operator=operator
        )
        self._session_open = True
        self._dropped_no_writer.clear()

        self._label_ts = [int(time.time() * 1000)]
        self._label_val = [(_DEFAULT_LABEL_ID, _DEFAULT_LABEL_NAME)]

        for device_id, role in device_roles.items():
            await self._open_writer_for(device_id, role)

    async def write_packet(self, pkt: SensorPacket) -> None:
        writer = self._writers.get(pkt.device_id) or self._rescue_writers.get(pkt.device_id)
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
            await audit.log("ERROR", "csv_write_error", {"error": str(exc), "device_id": pkt.device_id})
            # Try rescue path
            if pkt.device_id not in self._rescue_writers:
                await audit.log("ERROR", "no_rescue_writer", {"device_id": pkt.device_id})
            else:
                await self._rescue_writers[pkt.device_id].write_row(row)

    async def write_late(self, pkt: SensorPacket, role: str) -> None:
        """Append a post-STOP packet to <session>_<role>_sensor_data_late.csv."""
        if not self.late_session_id or self._late_base is None:
            return
        writer = self._late_writers.get(pkt.device_id)
        if writer is None:
            path = self._late_base / f"{self._late_session_id}_{role}_sensor_data_late.csv"
            writer = DeviceWriter(path, self._late_metadata + ",late_delivery=1")
            await writer.open(append_if_exists=True)
            self._late_writers[pkt.device_id] = writer
            await audit.log("WARN", "late_delivery_started",
                            {"device_id": pkt.device_id, "role": role, "path": str(path),
                             "session_id": self._late_session_id})
        await writer.write_row(_format_row(pkt, *self.label_at(pkt.timestamp_ms)))
        self._late_rows[pkt.device_id] = self._late_rows.get(pkt.device_id, 0) + 1

    async def close_session(self) -> dict:
        results = {}
        for device_id, writer in {**self._writers, **self._rescue_writers}.items():
            results[device_id] = await writer.close()

        # Arm the late-delivery window: a phone that reconnects within LATE_ACCEPT_SEC of
        # STOP still gets its buffered tail written, to a sidecar (plan DD-4).
        self._late_session_id = self._session_id
        self._late_base = self._base
        self._late_metadata = self._metadata_line
        self._late_closed_at = time.monotonic()
        self._late_rows = {}

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
            summary["devices"][device_id] = {
                **(await w.close()),
                "rows_appended": self._late_rows.get(device_id, 0),
            }
        if summary["devices"] and self._late_base is not None:
            (self._late_base / f"{self._late_session_id}_late_delivery.json").write_text(
                json.dumps(summary, indent=2)
            )
            await audit.log("WARN", "late_delivery_finalized", summary)
        self._late_writers.clear()
        self._late_rows.clear()
        self._late_session_id = ""
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
