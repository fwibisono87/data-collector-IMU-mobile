"""Filenames must not claim a sampling rate, and legacy names that do must still parse.

Files used to be written as `<session>_<role>_<tier>hz_sensor_data.csv`. The token was
derived from true_sensor_hz — distinct hardware readings per second — while the rows in the
file are emitted by a separate ~100 Hz timer. It therefore described neither the row cadence
nor a uniform grid, and the most natural way to read such a name is the one that is wrong:

    session 1786677865027, chest -> `chest_75hz_sensor_data.csv`
    56,173 rows spanning 575.890 s  =  97.54 rows/s
    rows / 75 = 748.9 s, against a real 575.9 s — a 30% error

Three phones in that session produced 56,173 / 50,569 / 57,317 rows over the SAME
wall-clock window (spans within 30 ms of each other), because each one's emit timer slipped
differently. No single rate in a filename can express that; the per-role timing sidecar
states it explicitly instead.

These tests pin both halves: new files carry no rate token, and old files on disk still
resolve to the right role.
"""
import asyncio
import json
from pathlib import Path

from master_backend.app.csv_schema import strip_tier_token


# ── Legacy token parsing (sessions already on disk) ──────────────────────────

def test_strip_tier_token_removes_only_the_token():
    assert strip_tier_token("waist_100hz") == "waist"
    assert strip_tier_token("thigh_right_75hz") == "thigh_right"
    assert strip_tier_token("chest_unkhz") == "chest"


def test_strip_tier_token_is_a_noop_on_untiered_names():
    """The shape every NEW file has."""
    assert strip_tier_token("waist") == "waist"
    assert strip_tier_token("thigh_right") == "thigh_right"


def test_role_from_name_handles_both_old_and_new_layouts():
    from master_backend.app.export import _role_from_name
    sid = "1786071998055"
    # Legacy, tiered — must keep resolving, or one device buckets under several roles.
    assert _role_from_name(f"{sid}_thigh_right_75hz_sensor_data.csv", sid) == "thigh_right"
    assert _role_from_name(f"{sid}_waist_100hz_sensor_data.csv", sid) == "waist"
    assert _role_from_name(f"{sid}_chest_50hz_sensor_data_late.csv", sid) == "chest"
    # Current, untiered
    assert _role_from_name(f"{sid}_thigh_right_sensor_data.csv", sid) == "thigh_right"
    assert _role_from_name(f"{sid}_waist_sensor_data.csv", sid) == "waist"
    assert _role_from_name(f"{sid}_chest_sensor_data_late.csv", sid) == "chest"


def test_analyze_session_cli_strips_the_token_too():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from tools.analyze_session import _identity_from_filename
    sid, role, kind = _identity_from_filename(
        Path("1786071998055_thigh_right_75hz_sensor_data.csv"))
    assert (sid, role, kind) == ("1786071998055", "thigh_right", "sensor_data")


# ── End to end through the writer ────────────────────────────────────────────

def _run_session(tmp_path, role, open_hz, close_hz):
    """Open and close one session in a SINGLE event loop.

    aiofiles binds its handle to the loop that opened it, so splitting open and close across
    two asyncio.run() calls fails with 'Event loop is closed'.
    """
    from master_backend.app import io_manager as io_mod

    async def body():
        mgr = io_mod.IoManager()
        mgr._ssd_path = tmp_path
        kwargs = {} if open_hz is None else {"device_rates": {"DEV1": open_hz}}
        await mgr.open_session(
            session_id="S1", subject_name="Subj", session_tag="Tag", operator="Op",
            device_roles={"DEV1": role}, **kwargs,
        )
        rates = {} if close_hz is None else {"DEV1": close_hz}
        return await mgr.close_session(rates)

    return asyncio.run(body())


def test_written_filename_carries_no_rate_token(tmp_path, monkeypatch):
    """The whole point: the name must not claim a rate it cannot describe."""
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    folder = tmp_path / "Data_Riset_IMU" / "Subj_Tag"

    results = _run_session(tmp_path, "thigh_right", 99.0, 84.0)

    expected = folder / "S1_thigh_right_sensor_data.csv"
    assert expected.exists()
    assert results["DEV1"]["path"] == str(expected)
    # Whatever the measured rate, it must not appear in the name.
    assert not list(folder.glob("*hz*")), [p.name for p in folder.glob("*hz*")]


def test_no_rename_happens_at_close(tmp_path, monkeypatch):
    """The file opened under one name and closed under the same one.

    The old close path renamed to correct the tier, which could fail and leave a stale
    label on correct data. There is nothing to correct now.
    """
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    results = _run_session(tmp_path, "waist", 99.0, 51.0)   # would have crossed two tiers
    folder = tmp_path / "Data_Riset_IMU" / "Subj_Tag"
    assert (folder / "S1_waist_sensor_data.csv").exists()
    assert "retiered_from" not in results["DEV1"]


def test_unmeasured_device_still_gets_a_stable_name(tmp_path, monkeypatch):
    """No measurement used to mean 'unkhz' in the name; now it changes nothing."""
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    _run_session(tmp_path, "chest", None, None)
    folder = tmp_path / "Data_Riset_IMU" / "Subj_Tag"
    assert (folder / "S1_chest_sensor_data.csv").exists()
    assert not (folder / "S1_chest_unkhz_sensor_data.csv").exists()


def test_close_session_reports_the_measured_rate_instead_of_naming_with_it(tmp_path, monkeypatch):
    """The measurement is not lost — it moves out of the name and into the result."""
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    results = _run_session(tmp_path, "waist", 99.0, 87.81)
    assert results["DEV1"]["session_avg_hz"] == 87.81


# ── Timing sidecar ───────────────────────────────────────────────────────────

def test_timing_sidecar_states_the_rate_that_describes_the_rows(tmp_path):
    """row_hz must be rows/span, the number that was missing.

    Reproduces the real chest device: 56,173 rows over 575.890 s. A reader given only
    `75hz` computes 748.9 s; row_hz gives back the true 97.54.
    """
    from master_backend.app.sampling_analysis import analyse_device

    first_ts = 1786677865080
    rows = []
    # 97.54 rows/s over ~575.89 s, evenly spaced for the purposes of the average.
    n = 56173
    span_ms = 575890
    # Sample the series rather than build 56k rows; include the LAST index so the span is
    # exact (nominal_hz is span-based, so a short tail would understate it).
    indices = list(range(0, n, 1000))
    if indices[-1] != n - 1:
        indices.append(n - 1)
    for i in indices:
        ts = first_ts + round(i * span_ms / (n - 1))
        rows.append([str(ts), "0.1", "0.2", "0.3", "1.0", "2.0", "3.0",
                     "0", "0", str(i), "DEV-CHEST", "", "", "0"])
    stats = analyse_device(rows, device_id="DEV-CHEST", role="chest")

    assert stats["first_timestamp_ms"] == first_ts
    assert stats["last_timestamp_ms"] == first_ts + span_ms
    span_s = (stats["last_timestamp_ms"] - stats["first_timestamp_ms"]) / 1000
    assert abs(span_s - 575.890) < 0.01
    # The label that would have been written for this device was 75hz; the honest figure
    # for its ROW cadence is far from that.
    assert stats["nominal_hz"] > 0
