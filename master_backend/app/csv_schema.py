"""Shared CSV schema definitions for IMU telemetry data.

This module is the single source of truth for the CSV header constants and
column indices. Other modules (io_manager, upload, export, integrity_validator)
should import from here instead of duplicating the header string.
"""

import re

CSV_HEADER_V1 = (
    "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
    "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
    "label_id,label_name,sequence_number,device_id\n"
)

CSV_HEADER_V2 = (
    "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
    "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
    "label_id,label_name,sequence_number,device_id,"
    "acc_ts_ms,gyro_ts_ms,sample_kind\n"
)

CSV_HEADER = CSV_HEADER_V2      # what writers emit from now on
V1_WIDTH = 11
V2_WIDTH = 14

COL_TIMESTAMP_MS = 0
COL_ACC_X = 1
COL_ACC_Y = 2
COL_ACC_Z = 3
COL_GYRO_X = 4
COL_GYRO_Y = 5
COL_GYRO_Z = 6
COL_LABEL_ID = 7
COL_LABEL_NAME = 8
COL_SEQUENCE = 9
COL_DEVICE_ID = 10
COL_ACC_TS_MS = 11
COL_GYRO_TS_MS = 12
COL_SAMPLE_KIND = 13

_METADATA_RE = re.compile(r"(\w+)=([^,\s]+)")


def is_header_line(line: str) -> bool:
    """Return True if the line is a CSV header of ANY schema version.

    Deliberately version-agnostic so that an older file's header keeps being
    recognised even after new columns are added.
    """
    return line.lstrip().startswith("timestamp_ms")


def parse_row(line: str) -> list | None:
    """Return a normalised field list, or None if the line is not a data row."""
    if not line or not line.strip():
        return None
    if line.lstrip().startswith("#"):
        return None
    if is_header_line(line):
        return None
    # Strip the line terminator before splitting: callers that iterate a file object
    # rather than splitlines() would otherwise leave "\n" glued to the last field, and
    # merge dedup keys built from device_id would stop matching across read styles.
    fields = line.rstrip("\r\n").split(",")
    if len(fields) < V1_WIDTH:
        return None
    if len(fields) < V2_WIDTH:
        fields = fields + [""] * (V2_WIDTH - len(fields))
    return fields


def schema_version_of(fields: list) -> int:
    if len(fields) >= V2_WIDTH and fields[COL_SAMPLE_KIND] != "":
        return 2
    return 1


def _sanitize(value: str) -> str:
    """Make a value safe to embed in the metadata line.

    parse_metadata_line (and the Flutter-side equivalent in recovery_uploader.dart)
    both read pairs with `(\\w+)=([^,\\s]+)`, so whitespace truncates a value just as
    badly as a comma does. Collapse both to underscores, matching the convention
    already used for session folder names.
    """
    out = value.replace(",", "_")
    return "_".join(out.split())


def metadata_line(*, session_id: str, subject: str = "", operator: str = "",
                  role: str = "", device_id: str = "", nominal_hz: float | None = None,
                  device_model: str = "", app_version: str = "",
                  schema_version: int = 2, extra: dict | None = None) -> str:
    parts = [("session_id", session_id), ("schema_version", str(schema_version))]
    optional = (
        ("subject", subject),
        ("operator", operator),
        ("role", role),
        ("device_id", device_id),
        ("nominal_hz", nominal_hz),
        ("device_model", device_model),
        ("app_version", app_version),
    )
    for key, val in optional:
        if val not in ("", None):
            parts.append((key, str(val)))
    if extra:
        for key, val in extra.items():
            parts.append((key, str(val)))
    rendered = ",".join(f"{_sanitize(str(k))}={_sanitize(str(v))}" for k, v in parts)
    return "# " + rendered


def parse_metadata_line(line: str) -> dict:
    if not line or not line.lstrip().startswith("#"):
        return {}
    return dict(_METADATA_RE.findall(line))
