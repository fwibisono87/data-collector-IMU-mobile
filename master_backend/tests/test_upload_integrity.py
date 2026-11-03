"""Recovery upload protocol must never turn a corrupt or gapped file into an exportable one."""
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from master_backend.app import upload


def _request(body: bytes, *, offset: int, total: int, complete: bool = False, sha: str = "") -> Request:
    query = urlencode({"device_id": "device-1", "session_id": "session-1"}).encode()
    headers = [(b"x-offset", str(offset).encode()), (b"x-total", str(total).encode())]
    if complete:
        headers.append((b"x-complete", b"1"))
        headers.append((b"x-sha256", sha.encode()))

    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/upload/csv",
                    "query_string": query, "headers": headers}, receive)


def _payload(response) -> dict:
    return json.loads(response.body)


def test_upload_rejects_offset_gap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(upload, "RECOVERY_PATH", tmp_path)
    first = asyncio.run(upload.upload_csv(_request(b"abc", offset=0, total=6)))
    assert _payload(first)["received_bytes"] == 3

    gap = asyncio.run(upload.upload_csv(_request(b"def", offset=4, total=6)))
    assert gap.status_code == 409
    assert _payload(gap)["expected_offset"] == 3
    assert (tmp_path / "session-1" / "device-1.csv").read_bytes() == b"abc"


def test_upload_marks_complete_only_after_size_and_hash_verify(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(upload, "RECOVERY_PATH", tmp_path)
    body = b"verified recovery csv"
    with pytest.raises(HTTPException) as bad:
        asyncio.run(upload.upload_csv(_request(body, offset=0, total=len(body), complete=True, sha="bad")))
    assert bad.value.status_code == 422
    info = json.loads((tmp_path / "session-1" / "device-1.info.json").read_text())
    assert info["complete"] is False
    assert info["state"] == "corrupt"

    # A corrupt terminal attempt remains resumable only by an explicit restart/repair; it is
    # never exposed as complete. Verify the happy path independently on a fresh device.
    request = Request({"type": "http", "method": "POST", "path": "/upload/csv",
                       "query_string": urlencode({"device_id": "device-2", "session_id": "session-1"}).encode(),
                       "headers": [(b"x-offset", b"0"), (b"x-total", str(len(body)).encode()),
                                   (b"x-complete", b"1"), (b"x-sha256", hashlib.sha256(body).hexdigest().encode())]},
                      lambda: __import__("asyncio").sleep(0, result={"type": "http.request", "body": body, "more_body": False}))
    verified = asyncio.run(upload.upload_csv(request))
    assert _payload(verified)["complete"] is True
    assert _payload(verified)["sha256_verified"] is True
