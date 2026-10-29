"""Generate ``schema.json`` from the Pydantic models in ``schema.py``.

Run from the repository root::

    python -m master_backend.app.api.export_schema
    python -m master_backend.app.api.export_schema --check

``--check`` exits non-zero (with a message) when the committed file no longer matches
the models; the backend test suite runs that check in-process so a schema edit without
a regeneration cannot be merged.

The document is a draft 2020-12 JSON Schema whose ``$defs`` hold one definition per
model and per WebSocket union. Response and server→client models are exported in
``serialization`` mode (that is what the dashboard receives); request and
client→server models are exported in ``validation`` mode (that is what the backend
accepts). Shared models appear once, in the mode of the direction they describe.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema

from . import schema

SCHEMA_PATH = Path(__file__).with_name("schema.json")

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
TITLE = "IMU Collector dashboard API"
DESCRIPTION = (
    "Generated from master_backend/app/api/schema.py. Do not edit by hand; "
    "run python -m master_backend.app.api.export_schema."
)

#: Models the backend sends (responses and server→client messages): serialization mode.
RESPONSE_MODELS: tuple[str, ...] = (
    "AckMsg",
    "CalibrationState",
    "Camera",
    "CameraDevice",
    "CameraSelectionResponse",
    "CompareCamera",
    "CompareResult",
    "DeviceList",
    "DiskInfo",
    "Drift",
    "DriftMsg",
    "EmptyResponse",
    "ErrorBody",
    "ErrorResponse",
    "FinalizeMsg",
    "FinalizeProgress",
    "FinalizeStep",
    "Issue",
    "LabelResponse",
    "LateRows",
    "LiveData",
    "LiveMsg",
    "LiveSample",
    "Notice",
    "PackageResponse",
    "Phone",
    "Readiness",
    "Reference",
    "ReferenceCamera",
    "ReferenceList",
    "SessionDetail",
    "SessionFile",
    "SessionInfo",
    "SessionLabel",
    "SessionList",
    "SessionSummary",
    "Snapshot",
    "SnapshotMsg",
    "StartResponse",
    "StopResponse",
    "UploadStatus",
    "Draft",
)

#: Models the backend accepts (request bodies and client→server messages): validation.
REQUEST_MODELS: tuple[str, ...] = (
    "CameraSelectionItem",
    "CameraSelectionRequest",
    "DraftUpdate",
    "LabelRequest",
    "ReferenceCreate",
    "ReferenceRename",
    "StartRequest",
    "StopRequest",
    "UnwatchMsg",
    "WatchMsg",
    "PingMsg",
)

#: Annotated discriminated unions, exported through a TypeAdapter with this mode.
UNION_MODELS: dict[str, str] = {
    "ServerMessage": "serialization",
    "ClientMessage": "validation",
}


def _entries() -> list[tuple[object, str]]:
    """``(model, mode)`` pairs in a stable order."""
    entries: list[tuple[object, str]] = []
    for name in RESPONSE_MODELS:
        entries.append((getattr(schema, name), "serialization"))
    for name in REQUEST_MODELS:
        entries.append((getattr(schema, name), "validation"))
    return entries


def build_document() -> dict:
    """Build the schema document (a fresh dict; no I/O)."""
    _, document = models_json_schema(
        _entries(),
        title=TITLE,
        description=DESCRIPTION,
        ref_template="#/$defs/{model}",
    )
    # models_json_schema() takes model classes only in this Pydantic version, so the
    # definitions it returns are already keyed by model name. Each model is exported in
    # exactly one mode, so there is no same-name collision between the two directions.
    definitions: dict[str, object] = dict(document.get("$defs", {}))

    # The discriminated unions are not models; export them through their TypeAdapters
    # so the dashboard can generate one union type per socket direction.
    for name, mode in UNION_MODELS.items():
        adapter = TypeAdapter(getattr(schema, name))
        union_schema = adapter.json_schema(mode=mode, ref_template="#/$defs/{model}")
        # The adapter re-emits every referenced model under its own "$defs"; those
        # definitions already live at the document root, so keep only the union itself.
        union_schema.pop("$defs", None)
        definitions[name] = union_schema

    return {
        "$schema": SCHEMA_DIALECT,
        "title": TITLE,
        "description": DESCRIPTION,
        "$defs": {name: definitions[name] for name in sorted(definitions)},
    }


def render() -> str:
    """The exact bytes of ``schema.json``: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(build_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check(path: Path = SCHEMA_PATH) -> list[str]:
    """Return human-readable differences; an empty list means the file is up to date."""
    try:
        on_disk = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path} cannot be read: {exc}"]
    expected = render()
    if on_disk == expected:
        return []
    return [
        f"{path} is stale: the committed schema differs from the models in "
        "master_backend/app/api/schema.py. Run "
        "`python -m master_backend.app.api.export_schema` and commit the result."
    ]


def write(path: Path = SCHEMA_PATH) -> None:
    path.write_text(render(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unexpected = [a for a in args if a != "--check"]
    if unexpected:
        print(f"unknown argument(s): {' '.join(unexpected)}", file=sys.stderr)
        print("usage: python -m master_backend.app.api.export_schema [--check]",
              file=sys.stderr)
        return 2

    if "--check" in args:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0

    write()
    print(f"wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())