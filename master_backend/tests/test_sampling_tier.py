"""Sampling-tier filename token: <session>_<role>_<tier>hz_sensor_data.csv.

The attained sampling rate is a property of the handset, not a setting — the 2510DRA23E is
dual-sourced and its Bosch units deliver ~80 Hz of distinct readings against a 100 Hz request.
The token records what a file actually contains. These tests pin the two things that can
silently corrupt downstream analysis: mislabelling a file as a higher rate than it delivered,
and letting the token leak into the parsed role so one device buckets under several names.
"""
import asyncio
from pathlib import Path

import pytest

from master_backend.app.csv_schema import (
    rate_tier,
    strip_tier_token,
    tier_token,
)
from master_backend.app.io_manager import _retier_name


# ── Tier ladder ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hz,expected", [
    (100.0, 100), (99.0, 100), (95.0, 100),        # 5% grace below nominal
    (94.9, 75), (88.0, 75), (84.0, 75), (75.0, 75), (71.25, 75),
    (71.0, 50), (50.0, 50), (47.5, 50),
    (47.4, 25), (25.0, 25), (23.75, 25),
    (23.7, 0), (1.0, 0), (0.0, 0),
])
def test_rate_tier_never_rounds_up(hz, expected):
    """A file must never claim a higher rate than it delivered."""
    assert rate_tier(hz) == expected


def test_rate_tier_handles_garbage():
    assert rate_tier(None) == 0
    assert rate_tier("nonsense") == 0


def test_tier_token_formats():
    assert tier_token(99.0) == "100hz"
    assert tier_token(84.0) == "75hz"
    assert tier_token(0.0) == "unkhz"


# ── Role parsing ─────────────────────────────────────────────────────────────

def test_strip_tier_token_removes_only_the_token():
    assert strip_tier_token("waist_100hz") == "waist"
    assert strip_tier_token("thigh_right_75hz") == "thigh_right"
    assert strip_tier_token("chest_unkhz") == "chest"


def test_strip_tier_token_is_a_noop_on_untiered_names():
    """Files written before this change carry no token and must still parse."""
    assert strip_tier_token("waist") == "waist"
    assert strip_tier_token("thigh_right") == "thigh_right"


def test_role_from_name_handles_both_old_and_new_layouts():
    from master_backend.app.export import _role_from_name
    sid = "1786071998055"
    # New, tiered
    assert _role_from_name(f"{sid}_thigh_right_75hz_sensor_data.csv", sid) == "thigh_right"
    assert _role_from_name(f"{sid}_waist_100hz_sensor_data.csv", sid) == "waist"
    assert _role_from_name(f"{sid}_chest_50hz_sensor_data_late.csv", sid) == "chest"
    # Legacy, untiered — sessions already on disk
    assert _role_from_name(f"{sid}_thigh_right_sensor_data.csv", sid) == "thigh_right"
    assert _role_from_name(f"{sid}_waist_sensor_data.csv", sid) == "waist"


def test_analyze_session_cli_strips_the_token_too():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from tools.analyze_session import _identity_from_filename
    sid, role, kind = _identity_from_filename(
        Path("1786071998055_thigh_right_75hz_sensor_data.csv"))
    assert (sid, role, kind) == ("1786071998055", "thigh_right", "sensor_data")


# ── Rename ───────────────────────────────────────────────────────────────────

def test_retier_name_preserves_roles_containing_underscores():
    assert _retier_name("123_thigh_right_50hz_sensor_data.csv", "75hz") == \
        "123_thigh_right_75hz_sensor_data.csv"
    assert _retier_name("123_waist_unkhz_sensor_data.csv", "100hz") == \
        "123_waist_100hz_sensor_data.csv"


def test_retier_name_covers_late_and_rescue_sidecars():
    assert _retier_name("123_chest_50hz_sensor_data_late.csv", "75hz") == \
        "123_chest_75hz_sensor_data_late.csv"
    assert _retier_name("123_chest_50hz_sensor_data_rescue.csv", "75hz") == \
        "123_chest_75hz_sensor_data_rescue.csv"


def test_retier_name_leaves_unrelated_files_alone():
    assert _retier_name("123_consolidated.csv", "75hz") == "123_consolidated.csv"


# ── End to end through the writer ────────────────────────────────────────────

def _run_session(tmp_path, role, open_hz, close_hz, on_open=None):
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
        if on_open:
            on_open()
        rates = {} if close_hz is None else {"DEV1": close_hz}
        return await mgr.close_session(rates)

    return asyncio.run(body())


def test_close_session_retiers_from_the_session_average(tmp_path, monkeypatch):
    """Named from preflight at open, corrected from the session average at close."""
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    folder = tmp_path / "Data_Riset_IMU" / "Subj_Tag"

    seen = {}
    # Preflight said ~99 Hz; the session actually sustained 84.
    results = _run_session(
        tmp_path, "thigh_right", 99.0, 84.0,
        on_open=lambda: seen.update(
            open_name=(folder / "S1_thigh_right_100hz_sensor_data.csv").exists()),
    )

    assert seen["open_name"], "file should be named from the preflight tier at open"
    assert not (folder / "S1_thigh_right_100hz_sensor_data.csv").exists()
    renamed = folder / "S1_thigh_right_75hz_sensor_data.csv"
    assert renamed.exists(), "file should have been re-tiered down to the attained rate"
    assert results["DEV1"]["path"] == str(renamed), "downstream must follow the new path"
    assert results["DEV1"]["retiered_from"] == "S1_thigh_right_100hz_sensor_data.csv"


def test_close_session_does_not_rename_when_the_tier_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    results = _run_session(tmp_path, "waist", 99.0, 97.0)   # both tier 100
    folder = tmp_path / "Data_Riset_IMU" / "Subj_Tag"
    assert (folder / "S1_waist_100hz_sensor_data.csv").exists()
    assert "retiered_from" not in results["DEV1"]


def test_unmeasured_device_is_labelled_unknown_not_guessed(tmp_path, monkeypatch):
    """No measurement must produce 'unkhz', never a fabricated rate."""
    monkeypatch.setenv("SSD_PATH", str(tmp_path))
    monkeypatch.setenv("SORT_CSV_ON_CLOSE", "false")
    folder = tmp_path / "Data_Riset_IMU" / "Subj_Tag"
    seen = {}
    _run_session(
        tmp_path, "chest", None, None,
        on_open=lambda: seen.update(
            open_name=(folder / "S1_chest_unkhz_sensor_data.csv").exists()),
    )
    assert seen["open_name"]
    assert (folder / "S1_chest_unkhz_sensor_data.csv").exists()
