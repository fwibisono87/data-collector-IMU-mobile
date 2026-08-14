"""Shared CSV schema definitions for IMU telemetry data.

This module is the single source of truth for the CSV header constants and
column indices. Other modules (io_manager, upload, export, integrity_validator)
should import from here instead of duplicating the header string.
"""

import math
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

# ── Sampling tier ────────────────────────────────────────────────────────────
#
# The rate a session ACTUALLY attained is a property of the handset, not a setting: the
# 2510DRA23E is dual-sourced, and the Bosch bmi3xy units deliver ~80 Hz of distinct readings
# against a 100 Hz request while the TDK icm4n607 units deliver ~99 Hz. Encoding the attained
# level in the filename lets an analyst see, and glob, what they are working with.
#
# Tiers are a coarse ladder rather than the raw figure: the raw value drifts session to
# session on the same device (79-88), so raw names would never group. The precise number stays
# in <session>_sampling.json and the integrity report.
#
# Floor semantics with a 5% grace: a file is only labelled 100hz if it genuinely sustained
# ~100. 99 -> 100hz (within grace), 88 -> 75hz, 84 -> 75hz, 50 -> 50hz. Never round up — the
# label must not claim more than the data delivered.
SAMPLING_TIERS = (100, 75, 50, 25)
_TIER_GRACE = 0.95
_TIER_RE = re.compile(r"_(?:\d+|unk)hz$")


def rate_tier(hz: float) -> int:
    """Highest tier this measured rate qualifies for, or 0 if below the ladder."""
    try:
        value = float(hz)
    except (TypeError, ValueError):
        return 0
    for tier in SAMPLING_TIERS:
        if value >= tier * _TIER_GRACE:
            return tier
    return 0


def tier_token(hz: float) -> str:
    """Filename token for a measured rate: '100hz', '75hz', ... or 'unkhz'."""
    tier = rate_tier(hz)
    return f"{tier}hz" if tier else "unkhz"


def strip_tier_token(stem: str) -> str:
    """Remove a trailing sampling-tier token from a filename stem.

    Every filename-to-role parser must call this, otherwise a role reads as 'waist_75hz' and
    per-role consolidation buckets the same physical device separately each time its attained
    rate shifts a tier.
    """
    return _TIER_RE.sub("", stem)


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


def is_valid_data_row(fields: list, *, expected_device_id: str | None = None) -> bool:
    """Validate the scalar fields required for an analysis-ready IMU row.

    ``parse_row`` intentionally remains a tolerant reader so old schema versions and
    recovery files can be inventoried without throwing.  The validator uses this stricter
    predicate before feeding rows to sampling analysis: a row with the right number of
    commas but a non-numeric timestamp, NaN sensor value, missing device id, or impossible
    provenance marker is not usable data and must be reported as malformed.
    """
    if len(fields) < V1_WIDTH:
        return False
    try:
        int(fields[COL_TIMESTAMP_MS])
        int(fields[COL_LABEL_ID])
        int(fields[COL_SEQUENCE])
        for index in (
            COL_ACC_X, COL_ACC_Y, COL_ACC_Z,
            COL_GYRO_X, COL_GYRO_Y, COL_GYRO_Z,
        ):
            value = float(fields[index])
            if not math.isfinite(value):
                return False
    except (TypeError, ValueError, IndexError):
        return False

    device_id = str(fields[COL_DEVICE_ID]).strip()
    if not device_id:
        return False
    if expected_device_id is not None and device_id != expected_device_id:
        return False

    acc_ts = fields[COL_ACC_TS_MS].strip() if len(fields) > COL_ACC_TS_MS else ""
    gyro_ts = fields[COL_GYRO_TS_MS].strip() if len(fields) > COL_GYRO_TS_MS else ""
    if acc_ts:
        try:
            int(acc_ts)
        except ValueError:
            return False
    if gyro_ts:
        try:
            int(gyro_ts)
        except ValueError:
            return False
    sample_kind = fields[COL_SAMPLE_KIND].strip() if len(fields) > COL_SAMPLE_KIND else ""
    return sample_kind in ("", "0", "1")


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
