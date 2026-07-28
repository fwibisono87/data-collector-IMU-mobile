"""
Async CSV writer with SSD fallback (CLAUDE.md §9.4).
One file handle per device per session. fsync every FSYNC_INTERVAL_SEC seconds.
"""
import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import aiofiles

from .audit_logger import audit
from master_backend.proto.sensor_packet import SensorPacket

logger = logging.getLogger(__name__)

_FSYNC_INTERVAL = int(os.getenv("FSYNC_INTERVAL_SEC", "5"))
_CSV_HEADER = (
    "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
    "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
    "label_id,label_name,sequence_number,device_id\n"
)
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

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = await aiofiles.open(self._path, mode="w", encoding="utf-8", newline="")
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
        sha256 = _sha256(self._path)
        return {
            "path": str(self._path),
            "rows": self._rows_written,
            "sha256": sha256,
        }


class IoManager:
    def __init__(self) -> None:
        self._writers: dict[str, DeviceWriter] = {}
        self._rescue_writers: dict[str, DeviceWriter] = {}
        self._active_label_id: int = _DEFAULT_LABEL_ID
        self._active_label_name: str = _DEFAULT_LABEL_NAME
        self._session_id: str = ""
        self._ssd_path: Path = Path(os.getenv("SSD_PATH", "./data"))
        self._rescue_path: Path = Path(os.getenv("RESCUE_PATH", "./data_rescue"))

    def set_label(self, label_id: int, label_name: str) -> None:
        self._active_label_id = label_id
        self._active_label_name = label_name

    @property
    def late_session_id(self) -> str:
        """Session still accepting late telemetry, or '' — advertised to phones on PONG.

        Stubbed to a constant here; T5 (plan C2) replaces the body with the real
        late-delivery window logic. Declared as a property from the start (not an
        __init__ attribute) to avoid an AttributeError when T5 turns it into a real
        property (plan R1).
        """
        return ""

    async def open_session(
        self,
        session_id: str,
        subject_name: str,
        session_tag: str,
        operator: str,
        device_roles: dict[str, str],  # device_id -> role
    ) -> None:
        self._session_id = session_id
        folder_name = f"{subject_name}_{session_tag}".replace(" ", "_")
        base = self._ssd_path / "Data_Riset_IMU" / folder_name
        metadata_line = (
            f"# session_id={session_id},subject={subject_name},"
            f"operator={operator},schema_version=1"
        )

        for device_id, role in device_roles.items():
            fname = f"{session_id}_{role}_sensor_data.csv"
            path = base / fname
            writer = DeviceWriter(path, metadata_line)
            try:
                await writer.open()
                self._writers[device_id] = writer
                await audit.log("INFO", "csv_opened", {"path": str(path), "device_id": device_id})
            except OSError as exc:
                await audit.log("ERROR", "ssd_write_failed", {"error": str(exc), "device_id": device_id})
                rescue_path = self._rescue_path / "Data_Riset_IMU" / folder_name / (fname.replace(".csv", "_rescue.csv"))
                rescue_writer = DeviceWriter(rescue_path, metadata_line)
                await rescue_writer.open()
                self._rescue_writers[device_id] = rescue_writer
                await audit.log("INFO", "rescue_path_activated", {"path": str(rescue_path)})

    async def write_packet(self, pkt: SensorPacket) -> None:
        writer = self._writers.get(pkt.device_id) or self._rescue_writers.get(pkt.device_id)
        if writer is None:
            return

        row = (
            f"{pkt.timestamp_ms},"
            f"{pkt.acc_x:.6f},{pkt.acc_y:.6f},{pkt.acc_z:.6f},"
            f"{pkt.gyro_x:.6f},{pkt.gyro_y:.6f},{pkt.gyro_z:.6f},"
            f"{self._active_label_id},{self._active_label_name},"
            f"{pkt.sequence_number},{pkt.device_id}\n"
        )
        try:
            await writer.write_row(row)
        except OSError as exc:
            await audit.log("ERROR", "csv_write_error", {"error": str(exc), "device_id": pkt.device_id})
            # Try rescue path
            if pkt.device_id not in self._rescue_writers:
                await audit.log("ERROR", "no_rescue_writer", {"device_id": pkt.device_id})
            else:
                await self._rescue_writers[pkt.device_id].write_row(row)

    async def close_session(self) -> dict:
        results = {}
        for device_id, writer in {**self._writers, **self._rescue_writers}.items():
            results[device_id] = await writer.close()
        self._writers.clear()
        self._rescue_writers.clear()
        self._active_label_id = _DEFAULT_LABEL_ID
        self._active_label_name = _DEFAULT_LABEL_NAME
        return results


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


io_manager = IoManager()
