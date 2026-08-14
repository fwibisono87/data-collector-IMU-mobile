"""
Phone recovery CSV upload + desktop pull endpoints.

Phones keep a complete local rescue CSV per session (LocalSessionRecorder). When the
WebSocket telemetry path drops for long stretches (or a session ends while the phone is
dark), that CSV is the authoritative copy. This module lets a phone upload that CSV to
the backend resumably over plain HTTP (robust to flaky Wi-Fi), then lets the operator
list, download and merge the recovery CSVs from the desktop dashboard — no adb needed.

Upload protocol (chunked, resumable):
  POST /upload/csv
    query:  device_id, session_id, role, subject, session_tag, operator
    header: X-Offset (byte offset this chunk starts at), X-Total (file size),
            X-Sha256 (full-file sha256; send on the final chunk),
            X-Complete (1 on the final chunk)
    body:   the raw chunk bytes

  GET  /upload/status?device_id=&session_id=  -> resume point (received_bytes)

The server appends chunks in order and tracks received_bytes per (device, session).
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .audit_logger import audit
from .csv_schema import (
    COL_DEVICE_ID,
    COL_SEQUENCE,
    CSV_HEADER as _CSV_HEADER,
    is_header_line,
    parse_row,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["recovery"])

RECOVERY_PATH = Path(os.getenv("RECOVERY_PATH", "./data_recovery"))
_upload_locks: dict[tuple[str, str], asyncio.Lock] = {}
_MAX_CHUNK_BYTES = 4 * 1024 * 1024


def _session_dir(session_id: str) -> Path:
    d = RECOVERY_PATH / (_slug(session_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)


def _info_path(session_id: str, device_id: str) -> Path:
    return _session_dir(session_id) / f"{_slug(device_id)}.info.json"


def _csv_path(session_id: str, device_id: str) -> Path:
    return _session_dir(session_id) / f"{_slug(device_id)}.csv"


def _default_info(session_id: str, device_id: str) -> dict:
    return {
        "session_id": session_id,
        "device_id": device_id,
        "role": "",
        "subject": "",
        "session_tag": "",
        "operator": "",
        "total_bytes": 0,
        "received_bytes": 0,
        "sha256": "",
        "complete": False,
        "done": False,
        "start_epoch_ms": int(time.time() * 1000),
        "updated_at_ms": int(time.time() * 1000),
    }


def _load_info(session_id: str, device_id: str) -> dict:
    p = _info_path(session_id, device_id)
    if not p.exists():
        return _default_info(session_id, device_id)
    try:
        base = _default_info(session_id, device_id)
        base.update(json.loads(p.read_text(encoding="utf-8")))
        return base
    except Exception:
        return _default_info(session_id, device_id)


def _save_info(info: dict) -> None:
    info["updated_at_ms"] = int(time.time() * 1000)
    path = _info_path(str(info["session_id"]), str(info["device_id"]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@router.get("/upload/status")
async def upload_status(
    device_id: str = Query(...),
    session_id: str = Query(...),
):
    info = _load_info(session_id, device_id)
    return info


@router.post("/upload/csv")
async def upload_csv(request: Request):
    device_id = (request.query_params.get("device_id") or "").strip()
    session_id = (request.query_params.get("session_id") or "").strip()
    role = (request.query_params.get("role") or "").strip()
    subject = (request.query_params.get("subject") or "").strip()
    session_tag = (request.query_params.get("session_tag") or "").strip()
    operator = (request.query_params.get("operator") or "").strip()
    if not device_id or not session_id:
        raise HTTPException(status_code=400, detail="device_id and session_id are required")

    offset = _int_header(request, "x-offset", 0)
    total = _int_header(request, "x-total", 0)
    sha = (request.headers.get("x-sha256") or "").strip()
    complete = request.headers.get("x-complete") == "1"

    body = await request.body()
    if offset < 0 or total < 0 or len(body) > _MAX_CHUNK_BYTES:
        raise HTTPException(status_code=400, detail="invalid upload offsets or chunk size")
    lock = _upload_locks.setdefault((session_id, device_id), asyncio.Lock())
    async with lock:
        info = _load_info(session_id, device_id)
        csv = _csv_path(session_id, device_id)
        actual = csv.stat().st_size if csv.exists() else 0
        expected = int(info.get("received_bytes", 0))
        if actual != expected:
            info["received_bytes"] = actual
            info["complete"] = False
            _save_info(info)
            expected = actual
        if offset != expected:
            return JSONResponse({**info, "expected_offset": expected}, status_code=409)
        prior_total = int(info.get("total_bytes", 0))
        if prior_total not in (0, total) or offset + len(body) > total:
            return JSONResponse({**info, "expected_offset": expected}, status_code=409)
        with open(csv, "ab") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        info.update({
            "role": role or info["role"], "subject": subject or info["subject"],
            "session_tag": session_tag or info["session_tag"], "operator": operator or info["operator"],
            "total_bytes": total, "received_bytes": offset + len(body), "complete": False,
            "sha256_verified": False,
        })
        if complete:
            if not sha or info["received_bytes"] != total or _sha256(csv) != sha:
                info["state"] = "corrupt"
                _save_info(info)
                raise HTTPException(status_code=422, detail="final size or sha256 verification failed")
            info.update({"sha256": sha, "sha256_verified": True, "complete": True, "state": "verified"})
            await audit.log("INFO", "recovery_upload_complete", {
                "session_id": session_id, "device_id": device_id, "bytes": info["received_bytes"],
            })
        else:
            info["state"] = "receiving"
        _save_info(info)
        return JSONResponse(info)


def _int_header(request: Request, name: str, default: int) -> int:
    try:
        return int(request.headers.get(name) or default)
    except ValueError:
        return default


def _merge_csv_to_output(
    sources: list[tuple[str, Path]],
    output: Path,
    *,
    metadata_prefix: str = "",
) -> dict:
    """Deduplicate and timestamp-sort sources with bounded Python memory.

    The previous implementation kept every row and dedup key in the Python heap. Long
    sessions could therefore fail during consolidation even when capture and the CSV
    writers had succeeded. SQLite stores the working set on disk and streams the final
    ordered file.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = output.with_name(output.name + ".merge.sqlite")
    try:
        db_path.unlink()
    except OSError:
        pass
    conn = sqlite3.connect(db_path)
    src_rows: dict[str, int] = {}
    source_files: list[dict] = []
    read_total = 0
    try:
        conn.execute(
            "CREATE TABLE rows (device_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "timestamp INTEGER NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY (device_id, sequence))"
        )
        conn.execute("CREATE INDEX rows_timestamp ON rows(timestamp)")
        for label, path in sources:
            if path is None or not path.exists():
                continue
            count = 0
            with open(path, "r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    fields = parse_row(line)
                    if fields is None:
                        continue
                    read_total += 1
                    try:
                        sequence = int(fields[COL_SEQUENCE])
                        timestamp = int(fields[0])
                    except (ValueError, IndexError):
                        continue
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO rows(device_id, sequence, timestamp, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (fields[COL_DEVICE_ID], sequence, timestamp, ",".join(fields)),
                    )
                    count += cursor.rowcount
                    if read_total % 2000 == 0:
                        conn.commit()
            conn.commit()
            src_rows[label] = src_rows.get(label, 0) + count
            source_files.append({"path": str(path), "label": label, "rows": count})

        row_count = int(conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0])
        tmp_output = output.with_name(output.name + ".tmp")
        with open(tmp_output, "w", encoding="utf-8", newline="") as target:
            if metadata_prefix:
                target.write(f"# {metadata_prefix}\n")
            target.write(_CSV_HEADER)
            for (payload,) in conn.execute("SELECT payload FROM rows ORDER BY timestamp, rowid"):
                target.write(payload + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp_output, output)
        return {
            "path": str(output),
            "rows": row_count,
            "sources": src_rows,
            "source_files": source_files,
            "duplicates_dropped": read_total - row_count,
        }
    finally:
        conn.close()
        try:
            db_path.unlink()
        except OSError:
            pass
        try:
            output.with_name(output.name + ".tmp").unlink()
        except OSError:
            pass


def merge_csv_sources(
    sources: list[tuple[str, Path]],
    output: Path,
    *,
    metadata_prefix: str = "",
) -> dict:
    return _merge_csv_to_output(sources, output, metadata_prefix=metadata_prefix)


def merge_csv_sources_per_role(
    sources: list[tuple[str, str, Path]],
    output_dir: Path,
    session_id: str,
    *,
    metadata_prefix: str = "",
) -> dict:
    """Merge CSVs into one <session_id>_<role>_consolidated.csv per role bucket.

    Per-role counterpart of merge_csv_sources: `sources` are (role_key, label, path)
    triples. Roles are bucketed independently, each deduped on (device_id, sequence_number)
    and re-sorted by timestamp, then written next to the session-wide consolidated file so
    operators can hand one device's full series (live + late + rescue + recovery) to a
    subject without the other devices' rows mixed in.
    """
    buckets: dict[str, list[tuple[str, Path]]] = {}
    for role_key, label, path in sources:
        if path is None or not path.exists():
            continue
        buckets.setdefault(role_key, []).append((label, path))

    output_dir.mkdir(parents=True, exist_ok=True)
    per_role: dict[str, dict] = {}
    written: list[str] = []
    for role_key, role_sources in sorted(buckets.items()):
        out_path = output_dir / f"{session_id}_{role_key}_consolidated.csv"
        result = _merge_csv_to_output(role_sources, out_path, metadata_prefix=metadata_prefix)
        per_role[role_key] = {
            "path": str(out_path),
            "rows": result["rows"],
            "sources": result["sources"],
            "source_files": result["source_files"],
            "duplicates_dropped": result["duplicates_dropped"],
        }
        written.append(str(out_path))

    return {
        "files": written,
        "roles": len(per_role),
        "rows": sum(p["rows"] for p in per_role.values()),
        "per_role": per_role,
    }


# ── Desktop pull ─────────────────────────────────────────────────────────────


@router.get("/recovery/sessions")
async def recovery_sessions(include_done: bool = False):
    """List recovery upload sessions.

    By default hides sessions whose files were all marked done (operator "Done"
    button). Pass `include_done=true` to see them again.
    """
    out = []
    if RECOVERY_PATH.exists():
        for d in sorted(RECOVERY_PATH.iterdir()):
            if not d.is_dir():
                continue
            infos = []
            for p in d.glob("*.info.json"):
                try:
                    value = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    infos.append(value)
            if not infos:
                continue
            all_done = all(i.get("done", False) for i in infos)
            if all_done and not include_done:
                continue
            out.append({
                "session_id": d.name,
                "files": infos,
                "done": all_done,
            })
    return out


def _set_done(session_id: str, done: bool) -> list[dict]:
    """Flip the operator `done` flag on every file of a recovery session."""
    d = _session_dir(session_id)
    updated = []
    for p in d.glob("*.info.json"):
        try:
            info = dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        info["done"] = done
        _save_info(info)
        updated.append(info.get("device_id"))
    return updated


@router.post("/recovery/{session_id}/dismiss")
async def recovery_dismiss(session_id: str):
    """Mark a recovery session as done so it stops cluttering the dashboard.

    Non-destructive: the CSVs remain on disk and can be restored / re-shown.
    """
    updated = _set_done(session_id, True)
    if not updated:
        raise HTTPException(status_code=404, detail="no recovery files for session")
    await audit.log("INFO", "recovery_dismissed", {"session_id": session_id, "devices": updated})
    return {"session_id": session_id, "done": True, "devices": updated}


@router.post("/recovery/{session_id}/restore")
async def recovery_restore(session_id: str):
    """Undo dismiss — bring a done recovery session back to the active list."""
    updated = _set_done(session_id, False)
    if not updated:
        raise HTTPException(status_code=404, detail="no recovery files for session")
    return {"session_id": session_id, "done": False, "devices": updated}


@router.get("/recovery/{session_id}/files")
async def recovery_files(session_id: str):
    d = _session_dir(session_id)
    infos = []
    for p in sorted(d.glob("*.info.json")):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(info, dict):
            continue
        info["file"] = p.name.replace(".info.json", ".csv")
        csv = d / (info["file"])
        info["size"] = csv.stat().st_size if csv.exists() else 0
        infos.append(info)
    return infos


@router.get("/recovery/{session_id}/files/{filename}")
async def recovery_download(session_id: str, filename: str):
    if not filename.endswith(".csv") or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    d = _session_dir(session_id)
    fp = d / filename
    if not fp.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(fp, media_type="text/csv", filename=f"{session_id}_{filename}")


@router.post("/recovery/{session_id}/merge")
async def recovery_merge(session_id: str):
    """Merge all complete recovery CSVs into <SSD_PATH>/Data_Riset_IMU/<subject>_<tag>/.

    Union of data rows keyed on (device_id, sequence_number), re-sorted by timestamp, written
    to <subject>_<tag>/<session_id>_merged.csv. Returns the merged file path and row counts.
    """
    d = _session_dir(session_id)
    sources: list[tuple[str, Path]] = []
    for p in d.glob("*.info.json"):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(info, dict):
            continue
        csv = d / f"{_slug(info['device_id'])}.csv"
        if csv.exists() and info.get("complete") and info.get("sha256_verified"):
            sources.append((info["device_id"], csv))
    if not sources:
        raise HTTPException(status_code=404, detail="no complete recovery files to merge")

    infos = []
    for p in d.glob("*.info.json"):
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            infos.append(value)
    subject = infos[0].get("subject", "Unknown") if infos else "Unknown"
    tag = infos[0].get("session_tag", "Session") if infos else "Session"

    ssd = Path(os.getenv("SSD_PATH", "./data")) / "Data_Riset_IMU" / f"{subject}_{tag}".replace(" ", "_")
    out_path = ssd / f"{session_id}_merged.csv"
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: merge_csv_sources(
            sources,
            out_path,
            metadata_prefix=(
                f"session_id={session_id},subject={subject},session_tag={tag},"
                "source=recovery_merge"
            ),
        ),
    )

    await audit.log("INFO", "recovery_merged", {
        "session_id": session_id, "devices": result["sources"], "rows": result["rows"],
        "path": result["path"],
    })
    return {
        "session_id": session_id,
        "path": result["path"],
        "rows": result["rows"],
        "devices": result["sources"],
        "duplicates_dropped": result["duplicates_dropped"],
    }
