"""The dashboard command channel must fail commands, never the connection.

Covers the two failure shapes that made a stop look like a crash:
  * an exception during a command used to escape into frontend_ws and close the socket,
    so the dashboard got no ACK and reported a timeout minutes later;
  * a repeated STOP used to be acknowledged as success and broadcast an empty integrity
    report, which overwrote the real verdict on every connected dashboard.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from master_backend.app import session_manager as sm_mod
from master_backend.app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from master_backend.app import session_ledger

    manager = sm_mod.session_manager
    monkeypatch.setattr(manager, "_state_path", tmp_path / ".sessions")
    monkeypatch.setattr(
        manager, "_ledger", session_ledger.SessionLedger(tmp_path / ".sessions")
    )
    manager.state = sm_mod.SessionState.IDLE
    manager.session_id = ""
    manager._devices.clear()
    # No context manager: exercise the routes without running the app lifespan
    # (mDNS, audit file, background reapers).
    return TestClient(app)


def drain(ws, limit: int = 6) -> list[dict]:
    """Read up to `limit` frames that are already queued, then stop.

    Every command this module sends produces an ACK, so the ACK is the terminator.
    """
    out: list[dict] = []
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        out.append(msg)
        if msg.get("type") == "ACK":
            break
    return out


def ack_of(frames: list[dict]) -> dict:
    acks = [f for f in frames if f.get("type") == "ACK"]
    assert acks, f"no ACK in {[f.get('type') for f in frames]}"
    return acks[-1]


def test_stop_from_idle_fails_the_command_not_the_socket(client):
    with client.websocket_connect("/ws/frontend") as ws:
        json.loads(ws.receive_text())          # initial snapshot

        ws.send_text(json.dumps({
            "type": "STOP_SESSION", "payload": {"reason": "operator_stop"},
            "command_id": "c1",
        }))
        ack = ack_of(drain(ws))
        assert ack["status"] == "fail"
        assert "RECORDING" in ack["detail"]

        # The socket is still usable — this is the whole point.
        ws.send_text(json.dumps({"type": "GET_STATE", "payload": {}, "command_id": "c2"}))
        assert json.loads(ws.receive_text())["type"] == "STATE_UPDATE"


def test_a_raising_command_does_not_close_the_connection(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("simulated handler failure")

    monkeypatch.setattr(sm_mod.session_manager, "reset_all", boom)

    with client.websocket_connect("/ws/frontend") as ws:
        json.loads(ws.receive_text())

        ws.send_text(json.dumps({"type": "RESET", "payload": {}, "command_id": "c1"}))
        ack = ack_of(drain(ws))
        assert ack["status"] == "fail"
        assert "simulated handler failure" in ack["detail"]

        # Still alive and answering.
        ws.send_text(json.dumps({"type": "GET_STATE", "payload": {}, "command_id": "c2"}))
        assert json.loads(ws.receive_text())["type"] == "STATE_UPDATE"


def test_state_snapshot_carries_finalize_progress(client):
    with client.websocket_connect("/ws/frontend") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "STATE_UPDATE"
        # Present (possibly null) so a dashboard connecting mid-finalize can render it.
        assert "finalize" in snapshot


def test_a_refused_stop_broadcasts_no_integrity_report(client):
    """The old handler ACKed ok and broadcast integrity_report: {} on a repeated stop."""
    with client.websocket_connect("/ws/frontend") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({
            "type": "STOP_SESSION", "payload": {}, "command_id": "c1",
        }))
        frames = drain(ws)
        assert ack_of(frames)["status"] == "fail"
        for msg in frames:
            if msg.get("type") == "STATE_UPDATE":
                assert not msg.get("integrity_report"), (
                    "a refused stop must not publish an empty report"
                )
