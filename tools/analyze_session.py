"""
Retrospective session analysis CLI for IMU telemetry.

The mobile app emits IMU packets from a fixed 100 Hz timer regardless of whether the
hardware sensor actually produced a new reading, so when a phone's sensor runs at
~50 Hz the previous reading is re-emitted with a fresh timestamp. Nothing in the
recorded file marks these synthetic (zero-order-hold) rows. This tool characterises
an already-recorded session after the fact, without modifying it, and reports how
much of each device's stream is held repeats plus an effective `true_hz`.

Usage (from repo root):
    python tools/analyze_session.py <path> [--json OUT] [--expected-hz N] [--quiet]

`<path>` may be a directory of session CSVs or a .zip export (extracted to a temp dir
for analysis).

Only `*_sensor_data.csv` and `*_consolidated.csv` are analysed (recursively);
`*_late.csv` and anything under a `recovery/` subdirectory are skipped as partial.

Exit code: 0 if every analysed file is PASS, 1 if any is PARTIAL or FAIL,
2 on usage/IO error (missing path, no matching files).
"""
import argparse
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from master_backend.app.csv_schema import parse_metadata_line, parse_row  # noqa: E402
from master_backend.app.sampling_analysis import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    analyse_device,
    classify,
)

_SENSOR_SUFFIX = "_sensor_data.csv"
_CONSOLIDATED_SUFFIX = "_consolidated.csv"
_LATE_SUFFIX = "_late.csv"
_RECOVERY_DIR = "recovery"
_COL_DEVICE_ID = 10


def _identity_from_filename(path: Path) -> tuple:
    """Derive (session_id, role, kind) from a `<session>_<role>_<kind>.csv` name."""
    name = path.name
    kind = None
    if name.endswith(_SENSOR_SUFFIX):
        prefix = name[: -len(_SENSOR_SUFFIX)]
        kind = "sensor_data"
    elif name.endswith(_CONSOLIDATED_SUFFIX):
        prefix = name[: -len(_CONSOLIDATED_SUFFIX)]
        kind = "consolidated"
    else:
        return "", "", kind
    parts = prefix.split("_")
    session_id = parts[0] if parts else ""
    role = "_".join(parts[1:]) if len(parts) > 1 else ""
    return session_id, role, kind


def _iter_target_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(_LATE_SUFFIX):
            continue
        rel = p.relative_to(root)
        if _RECOVERY_DIR in rel.parts:
            continue
        if name.endswith(_SENSOR_SUFFIX) or name.endswith(_CONSOLIDATED_SUFFIX):
            yield p


def _read_rows_and_meta(path: Path) -> tuple:
    """Read a CSV line-by-line into parsed rows plus merged metadata pairs.

    Memory note: sessions can reach ~25 MB / ~208k rows. We append each parsed row to
    a list (fine at this size) and never read the whole file as a single string.
    """
    rows = []
    meta = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.lstrip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                m = parse_metadata_line(line)
                if m:
                    meta.update(m)
                continue
            row = parse_row(line)
            if row is not None:
                rows.append(row)
    return rows, meta


def _device_groups(rows: list) -> list:
    """Split rows by device_id (col 10), preserving order within each group.

    A session-wide consolidated CSV merges several devices sorted by timestamp, so
    rows from different devices interleave. Splitting here keeps analyse_device
    per-device by contract: run-detection and sequence statistics are only valid on
    one device's stream at a time.
    """
    groups = {}
    order = []
    for row in rows:
        dev = row[_COL_DEVICE_ID] if len(row) > _COL_DEVICE_ID else ""
        if dev not in groups:
            groups[dev] = []
            order.append(dev)
        groups[dev].append(row)
    return [(dev, groups[dev]) for dev in order]


def _analyse_file(path: Path, expected_hz: float | None) -> list:
    rows, meta = _read_rows_and_meta(path)

    filename_session, filename_role, kind = _identity_from_filename(path)
    session_id = meta.get("session_id") or filename_session
    role = meta.get("role") or filename_role
    meta_device_id = meta.get("device_id", "")

    entries = []
    for dev, dev_rows in _device_groups(rows):
        # The row's device_id (col 10) is authoritative now that grouping keys on it;
        # fall back to the metadata value only when the column is blank.
        device_id = dev or meta_device_id
        stats = analyse_device(dev_rows, device_id=device_id, role=role,
                               expected_hz=expected_hz)
        verdict, reasons = classify(stats)
        stats["file"] = path.name
        stats["kind"] = kind
        stats["session_id"] = session_id
        stats["verdict"] = verdict
        stats["reasons"] = reasons
        entries.append(stats)
    return entries


def _analyse_root(root: Path, source: str, expected_hz: float | None) -> dict:
    files = list(_iter_target_files(root))
    if not files:
        print(f"error: no _sensor_data.csv or _consolidated.csv files under: {root}",
              file=sys.stderr)
        raise SystemExit(2)

    session_id = ""
    devices = []
    for path in files:
        for stat in _analyse_file(path, expected_hz):
            if not session_id and stat.get("session_id"):
                session_id = stat["session_id"]
            devices.append(stat)

    return {
        "session_id": session_id,
        "generated_at_ms": int(time.time() * 1000),
        "schema_version": 2,
        "source": source,
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "devices": devices,
    }


def analyse_path(path, expected_hz: float | None = None) -> dict:
    """Analyse a directory or .zip export and return the full result dict.

    Raises SystemExit(2) with a stderr message if the path is unreadable or matches
    no target CSVs.
    """
    src = Path(path)
    if not src.exists():
        print(f"error: path does not exist: {src}", file=sys.stderr)
        raise SystemExit(2)

    if src.is_file() and src.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(src) as zf:
                zf.extractall(td)
            return _analyse_root(Path(td), source=str(src), expected_hz=expected_hz)

    return _analyse_root(src, source=str(src), expected_hz=expected_hz)


def _print_table(result: dict) -> None:
    devices = result["devices"]
    show_declared = any(d.get("declared") is not None for d in devices)
    columns = ["file", "device", "rows", "span_s", "nominal", "true_hz", "held%",
               "miss", "gap", "verdict"]
    if show_declared:
        columns += ["decl%", "agree%"]

    def row_cells(d: dict) -> dict:
        cells = {
            "file": d["file"],
            "device": d["device_id"][:12],
            "rows": str(d["rows"]),
            "span_s": f"{d['span_s']:.1f}",
            "nominal": f"{d['nominal_hz']:.2f}",
            "true_hz": f"{d['true_sensor_hz']:.1f}",
            "held%": f"{d['held_row_pct']:.2f}",
            "miss": str(d["sequence"]["missing"]),
            "gap": str(d["sequence"]["largest_gap"]),
            "verdict": d["verdict"],
        }
        if show_declared:
            decl = d.get("declared")
            if decl:
                cells["decl%"] = f"{decl['held_row_pct_declared']:.2f}"
                cells["agree%"] = f"{decl['agreement_pct']:.1f}"
            else:
                cells["decl%"] = ""
                cells["agree%"] = ""
        return cells

    rows = [row_cells(d) for d in devices]
    widths = {}
    for col in columns:
        header_len = len(col)
        data_len = max((len(r[col]) for r in rows), default=0)
        widths[col] = max(header_len, data_len)

    def fmt(cell: str, col: str) -> str:
        return cell.ljust(widths[col]) if col in ("file", "device") \
            else cell.rjust(widths[col])

    print("  ".join(fmt(col, col) for col in columns))
    for r in rows:
        print("  ".join(fmt(r[col], col) for col in columns))


def _print_reasons(result: dict) -> None:
    for d in result["devices"]:
        if d["verdict"] != "PASS":
            for reason in d["reasons"]:
                print(f"  - {reason}")


def _print_footer(result: dict) -> None:
    affected = [d for d in result["devices"] if d["held_row_pct"] > 5]
    if not affected:
        return
    affected.sort(key=lambda d: d["held_row_pct"], reverse=True)
    parts = []
    for d in affected:
        label = d.get("role") or d["device_id"] or d["file"]
        parts.append(f"{label} {d['true_sensor_hz']:.1f} Hz of {d['nominal_hz']:.2f} "
                     f"nominal ({d['held_row_pct']:.1f}% held)")
    print()
    print("Note: held rows are zero-order-hold repeats of the previous hardware reading; the")
    print(f"effective rate is true_hz, not nominal. Worst affected: {', '.join(parts)}.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", help="directory of session CSVs or a .zip export")
    p.add_argument("--json", dest="json_out", default=None, metavar="OUT",
                   help="write the JSON report to OUT")
    p.add_argument("--expected-hz", type=float, default=None, metavar="N",
                   help="reference sample rate; defaults to each file's nominal Hz")
    p.add_argument("--quiet", action="store_true",
                   help="suppress the human-readable table")
    args = p.parse_args()

    result = analyse_path(args.path, expected_hz=args.expected_hz)

    if not args.quiet:
        _print_table(result)
        _print_reasons(result)
        _print_footer(result)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    if any(d["verdict"] in ("PARTIAL", "FAIL") for d in result["devices"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
