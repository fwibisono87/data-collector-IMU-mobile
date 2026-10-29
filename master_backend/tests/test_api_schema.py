"""The dashboard↔backend contract: shapes, discriminated unions, generated schema.

These tests are the reason the dashboard can generate its types instead of copying
API.md: every model is round-tripped through a representative example, the socket
unions are parsed by discriminator, and ``schema.json`` must match the models byte for
byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from master_backend.app.api import export_schema, schema  # noqa: E402

T0 = 1_793_351_700_000

DRIFT = {
    "verdict": "CHECK",
    "dx_px": 7.5,
    "dy_px": -2.25,
    "shift_pct": 0.59,
    "score": 0.71,
    "confidence": 0.93,
    "at_ms": T0 + 5_000,
}

PHONE = {
    "device_id": "dev-chest",
    "role": "chest",
    "model": "SM-A566B",
    "app_version": "3.0.0",
    "online": True,
    "offline_since_ms": None,
    "rate_hz": 100.2,
    "true_hz_avg": 98.9,
    "packets": 61_234,
    "telemetry_age_ms": 12,
    "buffering": False,
}

CAMERA = {
    "cam_id": "cam1",
    "device_key": "@device_pnp_\\\\?\\usb#vid_046d&pid_085c",
    "label": "C922 Pro Stream Webcam",
    "state": "live",
    "recording": True,
    "width": 1280,
    "height": 720,
    "fps": 30.0,
    "dropped_frames": 3,
    "error": None,
    "drift": DRIFT,
}

STEP = {
    "name": "cameras",
    "status": "running",
    "started_at_ms": T0 + 61_000,
    "ended_at_ms": None,
    "detail": None,
}

NOTICE = {
    "id": "ntc-1",
    "level": "warn",
    "code": "camera_lost",
    "message": "cam2 lost during recording",
    "session_id": "1793351700000",
    "created_at_ms": T0 + 62_000,
    "resolved": False,
}

SNAPSHOT = {
    "build_id": "a1b2c3d",
    "server_time_ms": T0 + 63_000,
    "state": "RECORDING",
    "session": {
        "session_id": "1793351700000",
        "subject": "S01",
        "session_tag": "walk",
        "operator": "francis",
        "started_at_ms": T0,
        "scheduled_start_ms": T0 - 500,
        "label": {"id": 4, "name": "walk", "since_ms": T0 + 30_000},
    },
    "last_session_id": None,
    "draft": {
        "subject": "S02",
        "session_tag": "walk",
        "operator": "francis",
        "updated_at_ms": T0 - 1_000,
    },
    "phones": [PHONE, {**PHONE, "device_id": "dev-waist", "role": "waist", "online": False,
                       "offline_since_ms": T0 + 62_000, "telemetry_age_ms": None}],
    "cameras": [CAMERA, {**CAMERA, "cam_id": "cam2", "drift": {**DRIFT, "verdict": "MOVED",
                                                               "dx_px": 24.0}}],
    "readiness": {
        "blockers": [],
        "warnings": [{"code": "camera_drift", "message": "cam2 moved 24 px",
                      "subject": "cam2"}],
    },
    "finalize": {
        "session_id": "1793351700000",
        "steps": [STEP, {**STEP, "name": "package", "status": "pending",
                         "started_at_ms": None}],
    },
    "notices": [NOTICE],
    "disk": {"path": "/mnt/ssd", "free_bytes": 512_000_000_000},
    "calibration": {"active_ref_id": "ref_20261030-091502_a3f1"},
}

SUMMARY = {
    "session_id": "1793351700000",
    "subject": "S01",
    "session_tag": "walk",
    "operator": "francis",
    "started_at_ms": T0,
    "ended_at_ms": T0 + 60_000,
    "duration_s": 60.0,
    "phones": ["chest", "waist"],
    "cameras": ["cam1", "cam2"],
    "verdict": "PARTIAL",
    "folder": "/mnt/ssd/Data_Riset_IMU/S01_walk",
    "zip_path": "/mnt/ssd/Data_Riset_IMU/S01_walk/S01_walk_1793351700000.zip",
    "zip_bytes": 734_003_200,
    "outstanding_uploads": 1,
    "notices": 2,
}

REFERENCE_CAMERA = {
    "device_key": "@device_pnp_\\\\?\\usb#vid_046d&pid_085c",
    "label": "C922 Pro Stream Webcam",
    "cam_id_at_capture": "cam1",
    "file": "device_pnp_usb_vid_046d_pid_085c.jpg",
    "width": 1280,
    "height": 720,
    "frame_arrival_ms": T0 + 987,
    "skew_ms": -13,
}

REFERENCE = {
    "ref_id": "ref_20261030-091502_a3f1",
    "name": "lab A — tripod layout",
    "created_at_ms": T0,
    "trigger_ms": T0 + 1_000,
    "cameras": [REFERENCE_CAMERA],
    "thumb_urls": {
        REFERENCE_CAMERA["device_key"]:
            "/api/calibration/references/ref_20261030-091502_a3f1/"
            "device_pnp_usb_vid_046d_pid_085c.jpg"
    },
}

COMPARE = {
    "ref_id": REFERENCE["ref_id"],
    "at_ms": T0 + 5_000,
    "cameras": [
        {**DRIFT, "device_key": CAMERA["device_key"], "cam_id": "cam1"},
        {**DRIFT, "verdict": "OK", "dx_px": 1.0, "dy_px": 0.5,
         "device_key": "fake:1", "cam_id": "cam2"},
    ],
}

LIVE_SAMPLE = {"acc": [0.01, -0.02, 0.98], "gyro": [1.5, -0.25, 0.0], "ts_ms": T0 + 10}

EXAMPLES: dict[str, dict] = {
    # shared
    "Issue": {"code": "no_camera_selected",
              "message": "Select at least one camera before starting.",
              "subject": "cameras"},
    "SessionLabel": {"id": 4, "name": "walk", "since_ms": T0 + 30_000},
    "SessionInfo": SNAPSHOT["session"],
    "Draft": SNAPSHOT["draft"],
    "Phone": PHONE,
    "CameraDevice": {"device_key": "fake:0", "label": "Fake Camera 0", "backend": "fake"},
    "Camera": CAMERA,
    "Drift": DRIFT,
    "FinalizeStep": STEP,
    "FinalizeProgress": SNAPSHOT["finalize"],
    "Notice": NOTICE,
    "Readiness": SNAPSHOT["readiness"],
    "DiskInfo": SNAPSHOT["disk"],
    "CalibrationState": SNAPSHOT["calibration"],
    "Snapshot": SNAPSHOT,
    "SessionSummary": SUMMARY,
    "SessionFile": {"name": "1793351700000_chest_sensor_data.csv", "bytes": 81_920,
                    "kind": "main"},
    "LateRows": {"device_id": "dev-waist", "role": "waist", "rows": 1_204},
    "UploadStatus": {"device_id": "dev-waist", "role": "waist",
                     "received_bytes": 4_096, "total_bytes": 8_192,
                     "complete": False, "verified": False},
    "SessionDetail": {
        **SUMMARY,
        "finalize": SNAPSHOT["finalize"]["steps"],
        "integrity_report": {"status": "PARTIAL", "analysis_ready": False},
        "files": [{"name": "1793351700000_chest_sensor_data.csv", "bytes": 81_920,
                   "kind": "main"}],
        "late": [{"device_id": "dev-waist", "role": "waist", "rows": 1_204}],
        "uploads": [{"device_id": "dev-waist", "role": "waist", "received_bytes": 4_096,
                     "total_bytes": 8_192, "complete": False, "verified": False}],
        "consolidation": {
            "state": "pending_phone_data",
            "detail": "1 rescue upload, 1,204 late rows waiting to be merged",
            "can_consolidate": True,
            "whole": False,
            "late_pending": True,
            "recovery_pending": True,
            "uploads_in_progress": False,
            "rescue_uploads": 1,
            "late_rows": 1_204,
        },
    },
    "ConsolidationStatus": {
        "state": "current",
        "detail": "current — every source is consolidated",
        "can_consolidate": False,
        "whole": False,
        "late_pending": False,
        "recovery_pending": False,
        "uploads_in_progress": False,
        "rescue_uploads": 1,
        "late_rows": 1_204,
    },
    "ReferenceCamera": REFERENCE_CAMERA,
    "Reference": REFERENCE,
    "CompareCamera": COMPARE["cameras"][0],
    "CompareResult": COMPARE,
    "ErrorBody": {"code": "blocked", "message": "Cannot start",
                  "details": {"blockers": [{"code": "no_phone",
                                            "message": "No phone online"}]}},
    "ErrorResponse": {"error": {"code": "not_found", "message": "No such session",
                                "details": None}},
    # REST requests
    "DraftUpdate": {"subject": "S02", "session_tag": None, "operator": "francis"},
    "StartRequest": {},
    "StopRequest": {"reason": "operator stopped"},
    "LabelRequest": {"label_id": 12},
    "CameraSelectionItem": {"device_key": "fake:1", "cam_id": "cam2"},
    "CameraSelectionRequest": {
        "cameras": [{"device_key": "fake:0"}, {"device_key": "fake:1", "cam_id": "cam3"}],
    },
    "ReferenceCreate": {"name": "lab A"},
    "ReferenceRename": {"name": "lab A — tripod layout"},
    # REST responses
    "StartResponse": {"session_id": "1793351700000", "scheduled_start_ms": T0 - 500,
                      "warnings": [{"code": "low_rate", "message": "chest at 82 Hz",
                                    "subject": "dev-chest"}]},
    "StopResponse": {"session_id": "1793351700000"},
    "LabelResponse": {"label_id": 12, "since_ms": T0 + 31_000},
    "SessionList": {"sessions": [SUMMARY], "next": None},
    "PackageResponse": {"zip_path": SUMMARY["zip_path"]},
    "CameraSelectionResponse": {"cameras": [CAMERA]},
    "DeviceList": {"devices": [{"device_key": "fake:0", "label": "Fake Camera 0",
                                "backend": "fake"}]},
    "ReferenceList": {"references": [REFERENCE], "active_ref_id": REFERENCE["ref_id"]},
    "EmptyResponse": {},
    # WebSocket server -> client
    "LiveSample": LIVE_SAMPLE,
    "LiveData": {"t_ms": T0 + 10, "phones": {"dev-chest": LIVE_SAMPLE}},
    "SnapshotMsg": {"type": "snapshot", "data": SNAPSHOT},
    "LiveMsg": {"type": "live",
                "data": {"t_ms": T0 + 10, "phones": {"dev-chest": LIVE_SAMPLE}}},
    "FinalizeMsg": {"type": "finalize", "data": SNAPSHOT["finalize"]},
    "DriftMsg": {"type": "calibration.drift", "data": COMPARE},
    "AckMsg": {"type": "ack", "req_id": "req-7", "ok": True, "error": None},
    # WebSocket client -> server
    "WatchMsg": {"type": "calibration.watch", "ref_id": "active", "req_id": "req-8"},
    "UnwatchMsg": {"type": "calibration.unwatch", "req_id": None},
    "PingMsg": {"type": "ping", "req_id": "req-9"},
}


def test_every_contract_model_has_an_example():
    """A new model must get a fixture here, so the round-trip coverage cannot rot.

    The two socket unions have no example of their own — their members are parsed by
    discriminator in ``test_server_message_discriminators`` and
    ``test_client_message_discriminators``.
    """
    covered = set(EXAMPLES)
    required = set(export_schema.RESPONSE_MODELS) | set(export_schema.REQUEST_MODELS)
    assert required - covered == set()
    assert covered - required == set()
    assert set(export_schema.UNION_MODELS) == {"ServerMessage", "ClientMessage"}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_model_round_trips(name):
    model = getattr(schema, name)
    parsed = model.model_validate(EXAMPLES[name])
    dumped = parsed.model_dump(mode="json")
    assert dumped == model.model_validate(dumped).model_dump(mode="json")


def test_snapshot_fixture_is_the_documented_shape():
    """Guard the fixture itself: it must exercise the interesting parts of Snapshot."""
    snapshot = schema.Snapshot.model_validate(SNAPSHOT)
    assert len(snapshot.phones) == 2
    assert len(snapshot.cameras) == 2
    assert all(cam.drift is not None for cam in snapshot.cameras)
    assert {cam.drift.verdict for cam in snapshot.cameras} == {"CHECK", "MOVED"}
    assert snapshot.finalize is not None and len(snapshot.finalize.steps) == 2
    assert len(snapshot.notices) == 1
    assert snapshot.state == "RECORDING"
    # Nested key names are part of the contract (the dashboard reads them verbatim).
    assert snapshot.model_dump(mode="json")["finalize"]["steps"][0]["name"] == "cameras"
    assert snapshot.model_dump(mode="json")["calibration"] == {
        "active_ref_id": REFERENCE["ref_id"]
    }


@pytest.mark.parametrize("payload", [
    EXAMPLES["SnapshotMsg"],
    EXAMPLES["LiveMsg"],
    EXAMPLES["FinalizeMsg"],
    EXAMPLES["DriftMsg"],
    EXAMPLES["AckMsg"],
])
def test_server_message_discriminators(payload):
    adapter = TypeAdapter(schema.ServerMessage)
    message = adapter.validate_python(payload)
    assert isinstance(message, getattr(schema, {
        "snapshot": "SnapshotMsg",
        "live": "LiveMsg",
        "finalize": "FinalizeMsg",
        "calibration.drift": "DriftMsg",
        "ack": "AckMsg",
    }[payload["type"]]))
    assert adapter.dump_python(message, mode="json") == payload


@pytest.mark.parametrize("payload", [
    EXAMPLES["WatchMsg"],
    EXAMPLES["UnwatchMsg"],
    EXAMPLES["PingMsg"],
])
def test_client_message_discriminators(payload):
    adapter = TypeAdapter(schema.ClientMessage)
    message = adapter.validate_python(payload)
    assert isinstance(message, getattr(schema, {
        "calibration.watch": "WatchMsg",
        "calibration.unwatch": "UnwatchMsg",
        "ping": "PingMsg",
    }[payload["type"]]))
    assert adapter.dump_python(message, mode="json") == payload


def test_client_messages_ignore_unknown_fields_but_reject_unknown_type():
    adapter = TypeAdapter(schema.ClientMessage)
    parsed = adapter.validate_python({
        "type": "calibration.watch",
        "ref_id": "active",
        "req_id": "req-1",
        "future_field": {"nested": True},
    })
    assert isinstance(parsed, schema.WatchMsg)
    assert not hasattr(parsed, "future_field")

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "calibration.something-new", "req_id": "req-2"})


def test_request_bodies_ignore_unknown_fields():
    for name in ("DraftUpdate", "StartRequest", "StopRequest", "LabelRequest",
                 "CameraSelectionRequest", "ReferenceCreate", "ReferenceRename"):
        payload = {**EXAMPLES[name], "not_in_the_contract_yet": 1}
        assert getattr(schema, name).model_validate(payload) is not None


@pytest.mark.parametrize("label_id,ok", [(0, True), (50, True), (51, False), (-1, False)])
def test_label_id_bounds(label_id, ok):
    if ok:
        assert schema.LabelRequest(label_id=label_id).label_id == label_id
    else:
        with pytest.raises(ValidationError):
            schema.LabelRequest(label_id=label_id)


def test_triple_float_and_camera_limit_are_enforced():
    with pytest.raises(ValidationError):
        schema.LiveSample.model_validate({"acc": [0.0, 0.0], "gyro": [0.0, 0.0, 0.0],
                                          "ts_ms": 1})
    with pytest.raises(ValidationError):
        schema.CameraSelectionRequest.model_validate({
            "cameras": [{"device_key": f"fake:{i}"} for i in range(6)]
        })


def test_schema_json_is_up_to_date_and_deterministic():
    assert export_schema.check() == []
    assert export_schema.render() == export_schema.render()
    on_disk = export_schema.SCHEMA_PATH.read_text(encoding="utf-8")
    assert on_disk.endswith("\n")
    assert on_disk == export_schema.render()


def test_check_reports_a_stale_file(tmp_path):
    stale = tmp_path / "schema.json"
    stale.write_text(export_schema.render() + " ", encoding="utf-8")
    problems = export_schema.check(stale)
    assert problems and "stale" in problems[0]


def test_schema_document_covers_every_model_and_all_refs_resolve():
    document = json.loads(export_schema.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    defs = document["$defs"]
    expected = (
        set(export_schema.RESPONSE_MODELS)
        | set(export_schema.REQUEST_MODELS)
        | set(export_schema.UNION_MODELS)
    )
    assert set(defs) == expected

    refs: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref":
                    refs.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(defs)
    for ref in refs:
        assert ref.startswith("#/$defs/")
        assert ref.removeprefix("#/$defs/") in defs


def test_schema_export_exposes_every_documented_enum():
    defs = json.loads(export_schema.SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]
    assert defs["FinalizeStep"]["properties"]["name"]["enum"] == [
        "close_writers", "cameras", "consolidate", "validate", "bundle", "package",
        "late_window"
    ]
    assert defs["FinalizeStep"]["properties"]["status"]["enum"] == [
        "pending", "running", "done", "failed", "skipped"
    ]
    assert defs["Drift"]["properties"]["verdict"]["enum"] == [
        "OK", "CHECK", "MOVED", "NO_SIGNAL", "MISSING", "RESOLUTION_CHANGED"
    ]
    assert defs["CameraDevice"]["properties"]["backend"]["enum"] == [
        "dshow", "v4l2", "fake"
    ]
    assert defs["Camera"]["properties"]["state"]["enum"] == [
        "opening", "live", "lost", "error"
    ]
    assert defs["Snapshot"]["properties"]["state"]["enum"] == [
        "IDLE", "RECORDING", "FINALIZING", "VALIDATING", "ERROR"
    ]
    assert defs["Notice"]["properties"]["level"]["enum"] == ["info", "warn", "error"]
    assert defs["SessionSummary"]["properties"]["verdict"]["enum"] == [
        "PASS", "PARTIAL", "FAIL", "UNKNOWN", "NONE"
    ]