"""Frozen output contract: every file a finished session leaves behind.

WHY this file exists
--------------------
Seventeen subjects have already been recorded.  The phone app, the dashboard and the
backend internals are about to be rebuilt, but the files on the SSD are the actual
research data and must keep exactly their current shape: same names, same metadata
lines, same header, same column order, same number formatting, same JSON keys.

This module drives the real modules (IoManager -> SessionManager -> FinalizeJob ->
IntegrityValidator -> export/upload/cameras) through one deterministic session, then
snapshots everything those modules wrote and compares it to a committed golden file.
It documents what the code DOES today, not what it should do; anything surprising is
frozen anyway and called out in docs/OUTPUT_CONTRACT.md.

Determinism
-----------
``time.time`` and ``time.monotonic`` are replaced with a clock the scenario advances
explicitly, so the session id (epoch ms) and every timestamp in every artifact are
fixed.  Values that still differ between two runs are listed in ``VOLATILE``.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from conftest import FakeWs
from master_backend.app.audit_logger import audit
from master_backend.app.cameras import cameras_mark
from master_backend.app.csv_schema import (
    COL_SAMPLE_KIND,
    CSV_HEADER_V2,
    parse_metadata_line,
)
from master_backend.app.dedup_store import dedup
from master_backend.proto.sensor_packet import SensorPacket

GOLDEN_PATH = Path(__file__).with_name("golden") / "session_snapshot.json"

# ── Fixed inputs ─────────────────────────────────────────────────────────────

# The clock starts here.  session_id is str(int(time.time() * 1000)) at START, so it
# is exactly this value.
T0 = 1_700_000_000_000
SESSION_ID = str(T0)

SUBJECT = "SUBJ_01"
SESSION_TAG = "TAG_A"
OPERATOR = "OP"
FOLDER_NAME = f"{SUBJECT}_{SESSION_TAG}"

CHEST = "11111111-1111-4111-8111-111111111111"
WAIST = "22222222-2222-4222-8222-222222222222"
THIGH = "33333333-3333-4333-8333-333333333333"

# (device_id, role) in registration order — this order is also the CSV/report order.
DEVICES = (
    (CHEST, "chest"),
    (WAIST, "waist"),
    (THIGH, "thigh_right"),
)

# The coordinator tells phones to start 500 ms ahead of the START command.
FIRST_PACKET_MS = T0 + 500
PACKETS = 600                       # ~6 s of 10 ms rows per device
STEP_MS = 10
GAP_DEVICE = CHEST                  # loses 20 sequence numbers mid-session
GAP_AT = 300                        # rows 300..319 are never delivered
GAP_SIZE = 20

CAMS = (
    # cam_id, device_id, browser_label, mime, started, flash, stopped
    ("cam1", CHEST, "Front", "video/webm", T0 + 100, T0 + 1_500, T0 + 6_495),
    ("cam2", WAIST, "Side", "video/webm", T0 + 150, T0 + 1_550, T0 + 6_498),
)

RECOVERY_ROWS = 20                  # waist rows the phone kept but the backend never saw


class FrozenClock:
    """A wall clock and a monotonic clock the scenario advances by hand."""

    def __init__(self, start_ms: int = T0) -> None:
        self._ms = start_ms

    @property
    def now_ms(self) -> int:
        return self._ms

    def advance(self, ms: int) -> int:
        self._ms += ms
        return self._ms

    def time(self) -> float:
        return self._ms / 1000.0

    def monotonic(self) -> float:
        return (self._ms - T0) / 1000.0


# ── Deterministic packet stream ──────────────────────────────────────────────


def _six(i: int) -> tuple[float, float, float, float, float, float]:
    """The physical reading for row ``i``.

    Held rows must repeat the previous six values — that is exactly what a held
    (OS-repeated) sample is, and what sampling_analysis detects independently.
    """
    if i and i % 97 == 0:
        i -= 1
    acc = round(math.sin(i / 10), 6)
    gyro = round(math.cos(i / 7) * 50, 6)
    return acc, acc, acc, gyro, gyro, gyro


def make_packet(device_id: str, index: int) -> SensorPacket:
    acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z = _six(index)
    ts = FIRST_PACKET_MS + index * STEP_MS
    sequence = index if not (device_id == GAP_DEVICE and index >= GAP_AT) else index + GAP_SIZE
    held = bool(index) and index % 97 == 0
    # i % 50 == 49 rows carry no sensor-clock stamp: the writer emits "" and never 0.
    blank_ts = index % 50 == 49
    return SensorPacket(
        acc_x=acc_x, acc_y=acc_y, acc_z=acc_z,
        gyro_x=gyro_x, gyro_y=gyro_y, gyro_z=gyro_z,
        timestamp_ms=ts, sequence_number=sequence, device_id=device_id,
        schema_version=2,
        acc_ts_ms=0 if blank_ts else ts,
        gyro_ts_ms=0 if blank_ts else ts - 2,
        sample_kind=1 if held else 0,
    )


async def deliver(rig, pkt: SensorPacket) -> None:
    """The RECORDING branch of ws_handler.telemetry_ws, without the socket.

    Kept in lock-step with ws_handler: dedup on (device, session, sequence), create a
    writer for a late joiner, count the packet, then write the row.
    """
    if dedup.is_duplicate(pkt.device_id, rig.manager.session_id, pkt.sequence_number):
        return
    dedup.add(pkt.device_id, rig.manager.session_id, pkt.sequence_number)
    if not rig.io.has_writer(pkt.device_id):
        dev = rig.manager.get_device(pkt.device_id)
        if dev is not None:
            await rig.io.ensure_writer(pkt.device_id, dev.device_role, dev.true_hz_avg)
    rig.manager.increment_packets(pkt.device_id)
    rig.manager.note_sample(pkt.device_id, (pkt.acc_x, pkt.acc_y, pkt.acc_z))
    rig.manager.mark_first_packet(pkt.device_id, pkt.timestamp_ms)
    await rig.io.write_packet(pkt)


async def deliver_late(rig, pkt: SensorPacket, role: str) -> None:
    """The post-STOP branch of ws_handler.telemetry_ws (late-delivery window)."""
    late_sid = rig.io.late_session_id
    assert late_sid, "the late window is not open"
    if dedup.is_duplicate(pkt.device_id, late_sid, pkt.sequence_number):
        return
    dedup.add(pkt.device_id, late_sid, pkt.sequence_number)
    await rig.io.write_late(pkt, role)


# ── The rescue CSV a phone writes ────────────────────────────────────────────


def rescue_csv(session_id: str, device_id: str, role: str) -> bytes:
    header = (
        f"# session_id={session_id},role={role},device_id={device_id},"
        f"subject={SUBJECT},session_tag={SESSION_TAG},operator={OPERATOR},"
        f"start_epoch_ms={FIRST_PACKET_MS},source=local_node,schema_version=2\n"
    )
    rows = []
    for index in range(PACKETS, PACKETS + RECOVERY_ROWS):
        acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z = _six(index)
        ts = FIRST_PACKET_MS + index * STEP_MS
        rows.append(
            f"{ts},{acc_x:.6f},{acc_y:.6f},{acc_z:.6f},"
            f"{gyro_x:.6f},{gyro_y:.6f},{gyro_z:.6f},0,0,{index},{device_id},{ts},{ts - 2},0"
        )
    return (header + CSV_HEADER_V2 + "\n".join(rows) + "\n").encode()


def _upload_request(body: bytes, *, device_id: str, session_id: str, role: str) -> Request:
    query = urlencode({
        "device_id": device_id, "session_id": session_id, "role": role,
        "subject": SUBJECT, "session_tag": SESSION_TAG, "operator": OPERATOR,
    }).encode()
    headers = [
        (b"x-offset", b"0"),
        (b"x-total", str(len(body)).encode()),
        (b"x-complete", b"1"),
        (b"x-sha256", hashlib.sha256(body).hexdigest().encode()),
    ]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/upload/csv",
         "query_string": query, "headers": headers},
        receive,
    )


# ── The scenario ─────────────────────────────────────────────────────────────


async def run_scenario(rig, monkeypatch) -> dict:
    """Produce one session containing every artifact the backend can write."""
    clock = FrozenClock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    # main.py opens the JSONL audit log at SSD_PATH/backend_audit.jsonl at startup.
    await audit.open(rig.ssd_path / "backend_audit.jsonl")

    for device_id, role in DEVICES:
        rig.manager.register_device(
            device_id=device_id, role=role, model="Pixel 7", app_version="2.3.0",
            ws=FakeWs(),
        )

    ok, session_id = await rig.manager.start_recording({
        "subject_name": SUBJECT, "session_tag": SESSION_TAG, "operator": OPERATOR,
    })
    assert ok, session_id
    assert session_id == SESSION_ID

    # Camera anchors arrive through the same coroutine the POST route calls.
    for cam_id, device_id, label, mime, started, flash, stopped in CAMS:
        for event, ts_ms in (("started", started), ("flash", flash)):
            await cameras_mark(session_id, {
                "cam_id": cam_id, "event": event, "ts_ms": ts_ms,
                "device_id": device_id, "label": label, "mime": mime,
            })

    # 0 -> 3 -> 7 -> 0 while the three streams run.  Each label change lands on a
    # distinct wall-clock ms, otherwise IoManager's monotonic guard folds them.
    batches = (
        (0, 200, None),                  # label 0 (the timeline's default at START)
        (200, 400, (2_500, 3, "walk")),
        (400, 520, (2_000, 7, "run")),
        (520, PACKETS, (1_200, 0, "0")),
    )
    for start, end, change in batches:
        if change is not None:
            advance_ms, label_id, label_name = change
            clock.advance(advance_ms)
            rig.io.set_label(label_id, label_name)
        for index in range(start, end):
            for device_id, _role in DEVICES:
                await deliver(rig, make_packet(device_id, index))

    for cam_id, device_id, label, mime, started, flash, stopped in CAMS:
        await cameras_mark(session_id, {
            "cam_id": cam_id, "event": "stopped", "ts_ms": stopped,
            "device_id": device_id, "label": label, "mime": mime,
        })

    clock.advance(800)                       # T0 + 6_500
    await rig.manager.request_stop("operator_stop")
    await rig.manager.wait_for_finalize()

    # A phone that was dark during STOP reconnects inside LATE_ACCEPT_SEC and flushes
    # its tail.  This is the write_late/late_delivery path (plan DD-4).
    clock.advance(500)                       # T0 + 7_000
    late = make_packet(GAP_DEVICE, PACKETS)   # sequence 620: the buffered tail
    await deliver_late(rig, late, "chest")
    await rig.io.finalize_late(force=True)

    # The waist phone also uploads its own rescue CSV (LocalSessionRecorder format),
    # carrying the rows the WebSocket missed.
    body = rescue_csv(session_id, WAIST, "waist")
    from master_backend.app.upload import upload_csv

    uploaded = await upload_csv(
        _upload_request(body, device_id=WAIST, session_id=session_id, role="waist")
    )
    assert json.loads(uploaded.body)["sha256_verified"] is True

    # Consolidate every source, then let the backend build the deliverable archive.
    await rig.export_module.export_consolidate(session_id)
    await rig.export_module.export_bundle(session_id)

    await audit.close()

    folder = rig.ssd_path / "Data_Riset_IMU" / FOLDER_NAME
    return {
        "session_id": session_id,
        "folder": folder,
        "snapshot": snapshot(
            folder,
            [
                rig.recovery_path / session_id,
                rig.rescue_path,
                rig.ssd_path / "Data_Riset_IMU" / "_pending_cameras",
                rig.ssd_path / ".sessions" / f"{session_id}.state.json",
                rig.ssd_path / "backend_audit.jsonl",
            ],
            session_id=session_id,
        ),
    }


# ── Snapshot ─────────────────────────────────────────────────────────────────
#
# Everything below is deliberately mechanical: names, metadata lines, headers,
# verbatim rows, whole JSON documents and zip entries.  A reviewer must be able to
# read the golden file and see exactly what an analyst would receive.

# Values that REALLY differ between two runs of run_scenario() and therefore get
# replaced with "<volatile>".  Nothing else is normalised: if a value is stable it
# belongs in the golden file.
VOLATILE = [
    # Absolute storage paths — SSD_PATH/RESCUE_PATH/RECOVERY_PATH are per-run tmp dirs.
    (r"^/", "<volatile>"),
]

SID_TOKEN = "<SID>"


def _csv_payload(text: str) -> dict:
    """Metadata lines, header, row count and the verbatim head/tail data lines."""
    lines = text.splitlines()
    metadata: list[str] = []
    index = 0
    while index < len(lines) and lines[index].startswith("#"):
        metadata.append(lines[index])
        index += 1
    header = lines[index] if index < len(lines) else ""
    rows = [line for line in lines[index + 1:] if line != ""]
    return {
        "metadata_lines": metadata,
        "header": header,
        "row_count": len(rows),
        "first_rows": rows[:5],
        "last_rows": rows[-5:],
    }


def _jsonl_payload(text: str) -> dict:
    """Line count and the key set of each line type (the audit event name).

    Only the shape is frozen.  Audit detail values embed absolute storage paths and
    content-derived bundle sizes, which legitimately differ between two runs of the
    same session; freezing them would mean normalising almost every line.
    """
    key_sets: dict[str, list[str]] = {}
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        count += 1
        event = json.loads(line)
        key_sets.setdefault(str(event.get("event", "")), sorted(event))
    return {"line_count": count, "key_sets": dict(sorted(key_sets.items()))}


def _zip_payload(path: Path) -> dict:
    out: dict = {"entries": [], "csv": {}, "json": {}, "jsonl": {}}
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            data = archive.read(name)
            out["entries"].append(name)
            if name.endswith(".csv"):
                out["csv"][name] = _csv_payload(data.decode("utf-8", "replace"))
            elif name.endswith(".json"):
                out["json"][name] = json.loads(data)
            elif name.endswith(".jsonl"):
                out["jsonl"][name] = _jsonl_payload(data.decode("utf-8", "replace"))
    return out


def _collect_files(session_folder: Path, extra_paths) -> dict[str, Path]:
    found: dict[str, Path] = {}

    def add(path: Path) -> None:
        if path.name in found and found[path.name] != path:
            raise AssertionError(f"two artifacts share the name {path.name!r}")
        found[path.name] = path

    for root in (Path(session_folder), *[Path(p) for p in extra_paths]):
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    add(path)
        elif root.is_file():
            add(root)
    return found


def snapshot(session_folder, extra_paths=(), *, session_id: str | None = None) -> dict:
    """Serialise every artifact under ``session_folder`` and ``extra_paths``."""
    files = _collect_files(session_folder, extra_paths)
    out: dict = {"files": sorted(files), "csv": {}, "json": {}, "jsonl": {}, "zip": {}}
    for name in sorted(files):
        path = files[name]
        if name.endswith(".csv"):
            out["csv"][name] = _csv_payload(path.read_text(encoding="utf-8", errors="replace"))
        elif name.endswith(".json"):
            out["json"][name] = json.loads(path.read_text(encoding="utf-8"))
        elif name.endswith(".jsonl"):
            out["jsonl"][name] = _jsonl_payload(
                path.read_text(encoding="utf-8", errors="replace")
            )
        elif name.endswith(".zip"):
            out["zip"][name] = _zip_payload(path)
    return normalise(out, session_id)


def _normalise_string(value: str, session_id: str | None) -> str:
    if session_id:
        value = value.replace(session_id, SID_TOKEN)
    for pattern, replacement in VOLATILE:
        if re.search(pattern, value):
            return replacement
    return value


def normalise(value, session_id: str | None = None):
    """Replace the session id, then any value matching a VOLATILE rule."""
    if isinstance(value, str):
        return _normalise_string(value, session_id)
    if isinstance(value, list):
        return [normalise(item, session_id) for item in value]
    if isinstance(value, dict):
        return {
            normalise(key, session_id): normalise(item, session_id)
            for key, item in value.items()
        }
    return value


def pretty(document) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True)


def unified_diff(expected: str, actual: str) -> str:
    diff = list(difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile="golden/session_snapshot.json", tofile="actual snapshot", lineterm="",
    ))
    limit = 400
    if len(diff) > limit:
        diff = diff[:limit] + [f"... ({len(diff) - limit} more diff lines)"]
    return "\n".join(diff)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _drive(rig, monkeypatch) -> dict:
    """Run the whole scenario on one rig, synchronously (pytest-asyncio is strict)."""
    import asyncio

    return asyncio.run(run_scenario(rig, monkeypatch))


@pytest.fixture
def scenario(rig, monkeypatch):
    return _drive(rig, monkeypatch)

# ── Format-level assertions (independent of the golden file) ────────────────
#
# These are the tests that should still say WHAT broke when a future
# implementation changes a name, a header, a column or a JSON key tree and then
# regenerates the golden file to make the diff go away.

FLOAT6 = re.compile(r"^-?\d+\.\d{6}$")
INTEGER = re.compile(r"^-?\d+$")
OPTIONAL_INTEGER = re.compile(r"^-?\d*$")

# column index -> (name, pattern). Mirrors io_manager._format_row exactly.
COLUMN_FORMATS = (
    (0, "timestamp_ms", INTEGER),
    (1, "acc_x_g", FLOAT6),
    (2, "acc_y_g", FLOAT6),
    (3, "acc_z_g", FLOAT6),
    (4, "gyro_x_degs", FLOAT6),
    (5, "gyro_y_degs", FLOAT6),
    (6, "gyro_z_degs", FLOAT6),
    (7, "label_id", INTEGER),
    (8, "label_name", re.compile(r"^[^,\s]+$")),
    (9, "sequence_number", INTEGER),
    (10, "device_id", re.compile(r"^[0-9a-f-]{36}$")),
    (11, "acc_ts_ms", OPTIONAL_INTEGER),
    (12, "gyro_ts_ms", OPTIONAL_INTEGER),
    (13, "sample_kind", re.compile(r"^[01]$")),
)

EXPECTED_FILES = {
    "<SID>.state.json",
    "<SID>_bundle.zip",
    "<SID>_cameras.json",
    "<SID>_chest_consolidated.csv",
    "<SID>_chest_sensor_data.csv",
    "<SID>_chest_sensor_data_late.csv",
    "<SID>_chest_timing.json",
    "<SID>_connectivity.json",
    "<SID>_consolidated.csv",
    "<SID>_consolidated_validation.json",
    "<SID>_consolidation.json",
    "<SID>_integrity_report.json",
    "<SID>_late_delivery.json",
    "<SID>_sampling.json",
    "<SID>_thigh_right_consolidated.csv",
    "<SID>_thigh_right_sensor_data.csv",
    "<SID>_thigh_right_timing.json",
    "<SID>_waist_consolidated.csv",
    "<SID>_waist_sensor_data.csv",
    "<SID>_waist_timing.json",
    f"{WAIST}.csv",
    f"{WAIST}.info.json",
    "backend_audit.jsonl",
}


def _main_csv(folder: Path, session_id: str, role: str) -> Path:
    return folder / f"{session_id}_{role}_sensor_data.csv"


def test_every_artifact_type_is_produced(scenario):
    """The scenario must actually exercise every writer; a missing file is a hole
    in the contract, not a smaller golden file."""
    assert set(scenario["snapshot"]["files"]) == EXPECTED_FILES


def test_device_csv_name_header_and_cell_format(scenario):
    folder, sid = scenario["folder"], scenario["session_id"]

    # Filenames: <session_id>_<role>_sensor_data.csv. No sampling-tier token — the
    # current writer emits none (legacy files may still carry `_75hz`).
    assert not any(re.search(r"_\d+hz_", p.name) for p in folder.iterdir())

    for device_id, role in DEVICES:
        path = _main_csv(folder, sid, role)
        assert path.is_file(), f"missing {path.name}"
        lines = path.read_text(encoding="utf-8").splitlines()

        # Metadata: exactly one `#` line, session id first (csv_schema.metadata_line).
        assert lines[0].startswith("# ")
        meta = parse_metadata_line(lines[0])
        assert list(meta) == ["session_id", "schema_version", "subject", "operator"]
        assert meta == {
            "session_id": sid, "schema_version": "2",
            "subject": SUBJECT, "operator": OPERATOR,
        }

        # Header: byte-for-byte the schema constant (it already ends with \n).
        assert lines[1] == CSV_HEADER_V2.rstrip("\n")

        rows = lines[2:]
        assert len(rows) == PACKETS
        blank_stamps = 0
        for line in rows:
            fields = line.split(",")
            assert len(fields) == 14, f"expected 14 columns: {line}"
            for index, name, pattern in COLUMN_FORMATS:
                assert pattern.match(fields[index]), (
                    f"{role} column {index} ({name}) is {fields[index]!r}"
                )
            assert fields[10] == device_id
            if fields[11] == "" or fields[12] == "":
                # Zero means "no sensor-clock stamp"; the writer must emit "".
                blank_stamps += 1
                assert fields[11] == "" and fields[12] == ""
                assert fields[11] != "0" and fields[12] != "0"
        assert blank_stamps == PACKETS // 50

        # sample_kind 0/1 both appear and nothing else.
        kinds = {line.split(",")[COL_SAMPLE_KIND] for line in rows}
        assert kinds == {"0", "1"}


def test_rows_are_sorted_and_the_sequence_gap_is_visible(scenario):
    folder, sid = scenario["folder"], scenario["session_id"]
    for _device_id, role in DEVICES:
        timestamps = [
            int(line.split(",", 1)[0])
            for line in _main_csv(folder, sid, role).read_text().splitlines()[2:]
            if line
        ]
        assert timestamps == sorted(timestamps), f"{role} CSV is not timestamp-sorted"

    chest = _main_csv(folder, sid, "chest").read_text().splitlines()[2:]
    sequences = {int(line.split(",")[9]) for line in chest}
    assert sequences == set(range(300)) | set(range(320, PACKETS + GAP_SIZE))
    assert len(sequences) == PACKETS

    # And the stop-time report says so.
    report = json.loads(
        (folder / f"{sid}_integrity_report.json").read_text(encoding="utf-8")
    )
    chest_report = next(d for d in report["devices"] if d["device_id"] == CHEST)
    assert chest_report["sampling"]["sequence"]["missing"] == GAP_SIZE
    assert chest_report["sampling"]["sequence"]["largest_gap"] == GAP_SIZE


def test_json_report_top_level_keys(scenario):
    """Pin the key tree a future implementer must keep emitting."""
    folder, sid = scenario["folder"], scenario["session_id"]
    documents = {
        name: json.loads((folder / name).read_text(encoding="utf-8"))
        for name in (
            f"{sid}_integrity_report.json",
            f"{sid}_connectivity.json",
            f"{sid}_sampling.json",
            f"{sid}_consolidation.json",
            f"{sid}_consolidated_validation.json",
            f"{sid}_late_delivery.json",
            f"{sid}_cameras.json",
            f"{sid}_chest_timing.json",
        )
    }
    with zipfile.ZipFile(folder / f"{sid}_bundle.zip") as archive:
        documents["bundle_manifest.json"] = json.loads(
            archive.read("bundle_manifest.json")
        )

    expected = {
        f"{sid}_integrity_report.json": {
            "session_id", "status", "validated_at_ms", "validation_scope",
            "label_timeline", "session_start_ms", "session_end_ms", "devices",
            "cross_device_checks", "analysis_ready", "analysis_ready_reasons",
        },
        f"{sid}_connectivity.json": {"session_id", "devices"},
        f"{sid}_sampling.json": {
            "session_id", "generated_at_ms", "schema_version", "thresholds", "devices",
        },
        f"{sid}_consolidation.json": {
            "session_id", "consolidated_at_ms", "per_role", "per_role_files",
            "path", "rows", "sources", "source_files", "duplicates_dropped",
            "validation",
        },
        f"{sid}_consolidated_validation.json": {
            "session_id", "status", "validated_at_ms", "validation_scope",
            "thresholds", "roles",
        },
        f"{sid}_late_delivery.json": {"session_id", "devices"},
        f"{sid}_cameras.json": {"session_id", "cameras"},
        f"{sid}_chest_timing.json": {
            "session_id", "role", "device_id", "csv", "first_ts_ms", "last_ts_ms",
            "span_s", "rows", "row_hz", "true_hz", "held_pct", "note",
        },
        "bundle_manifest.json": {
            "session_id", "built_at_ms", "entries", "lifecycle", "contains_video",
            "note",
        },
    }
    for name, keys in expected.items():
        assert set(documents[name]) == keys, name

    # The nested shapes the dashboard and the analyst read directly.
    assert set(documents[f"{sid}_cameras.json"]["cameras"][0]) == {
        "session_id", "cam_id", "device_id", "browser_label", "mime",
        "started_at_ms", "flash_at_ms", "stopped_at_ms", "track_ended_at_ms",
    }
    late_device = next(iter(documents[f"{sid}_late_delivery.json"]["devices"].values()))
    assert set(late_device) == {
        "path", "rows", "sha256", "reordered", "rows_appended",
    }


def test_label_timeline_is_frozen(scenario):
    """Labels are stamped with the wall clock at the moment SET_LABEL arrives."""
    sid = scenario["session_id"]
    report = json.loads(
        (scenario["folder"] / f"{sid}_integrity_report.json").read_text(encoding="utf-8")
    )
    assert report["label_timeline"] == [
        {"timestamp_ms": T0, "label_id": 0, "label_name": "0"},
        {"timestamp_ms": T0 + 2_500, "label_id": 3, "label_name": "walk"},
        {"timestamp_ms": T0 + 4_500, "label_id": 7, "label_name": "run"},
        {"timestamp_ms": T0 + 5_700, "label_id": 0, "label_name": "0"},
    ]


# ── Determinism and the golden file ─────────────────────────────────────────


def _run_once(rig_builder, monkeypatch, base) -> dict:
    with rig_builder(base) as rig:
        return _drive(rig, monkeypatch)


def _two_run_snapshots(rig_builder, monkeypatch, tmp_path):
    first = _run_once(rig_builder, monkeypatch, tmp_path / "run1")["snapshot"]
    second = _run_once(rig_builder, monkeypatch, tmp_path / "run2")["snapshot"]
    return first, second


def test_scenario_is_deterministic(rig_builder, monkeypatch, tmp_path):
    """Two independent runs, two tmp dirs: the snapshots must be identical.

    Anything that still differs belongs in VOLATILE and nowhere else.
    """
    first, second = _two_run_snapshots(rig_builder, monkeypatch, tmp_path)
    if first != second:
        raise AssertionError(
            "the scenario is not deterministic:\n"
            + unified_diff(pretty(first), pretty(second))
        )


def test_outputs_match_golden(scenario):
    actual = scenario["snapshot"]
    if os.getenv("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(pretty(actual) + "\n", encoding="utf-8")
        return
    assert GOLDEN_PATH.is_file(), (
        f"{GOLDEN_PATH} is missing — regenerate with "
        "UPDATE_GOLDEN=1 master_backend/.venv/bin/python -m pytest "
        "master_backend/tests/contract -q"
    )
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if actual != expected:
        raise AssertionError(
            "session outputs changed. If the change is intentional, re-read "
            "docs/OUTPUT_CONTRACT.md and regenerate the golden file with "
            "UPDATE_GOLDEN=1.\n" + unified_diff(pretty(expected), pretty(actual))
        )
