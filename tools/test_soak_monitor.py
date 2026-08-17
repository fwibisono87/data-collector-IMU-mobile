"""Unit tests for tools/soak_monitor.py (stdlib only, no pytest required).

Run from repo root: python tools/test_soak_monitor.py
"""
import argparse
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import soak_monitor as sm


def _write_plan(dir_path: str, obj: object) -> Path:
    path = Path(dir_path) / "plan.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _plan(overrides: dict | None = None, faults: object | None = None) -> dict:
    plan = {
        "duration_sec": 60,
        "poll_sec": 5,
        "startup_timeout_sec": 120,
        "package_name": "com.example.sensors_app",
        "faults": faults if faults is not None else [
            {"at_sec": 10, "serial": "phone-a", "action": "wifi_off",
             "duration_sec": 20},
            {"at_sec": 12, "serial": "phone-b", "action": "screen_off"},
        ],
    }
    if overrides:
        plan.update(overrides)
    return plan


class PlanValidationTest(unittest.TestCase):
    def test_valid_plan_loads_and_synthesizes_wifi_on(self):
        with tempfile.TemporaryDirectory() as td:
            plan, faults = sm.load_and_validate_plan(_write_plan(td, _plan()))
        self.assertEqual(plan["package_name"], "com.example.sensors_app")
        synced = [f for f in faults if f.action == "wifi_on" and f.source_index == 0]
        self.assertEqual(len(synced), 1)
        self.assertAlmostEqual(synced[0].at_sec, 30.0)
        self.assertEqual(synced[0].serial, "phone-a")

    def test_invalid_json_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(path)

    def test_missing_plan_file_rejected(self):
        with self.assertRaises(sm.PlanError):
            sm.load_and_validate_plan("does-not-exist.json")

    def test_unknown_action_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, _plan(faults=[
                    {"at_sec": 1, "serial": "a", "action": "clear_data"}])))

    def test_missing_serial_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, _plan(faults=[
                    {"at_sec": 1, "action": "wifi_on"}])))

    def test_negative_at_sec_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, _plan(faults=[
                    {"at_sec": -5, "serial": "a", "action": "wifi_on"}])))

    def test_non_positive_fault_duration_rejected(self):
        plan = _plan()
        plan["faults"][0]["duration_sec"] = 0
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, plan))

    def test_non_positive_poll_sec_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, _plan({"poll_sec": -1})))

    def test_duration_sec_only_valid_for_wifi_off(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, _plan(faults=[
                    {"at_sec": 1, "serial": "a", "action": "screen_off",
                     "duration_sec": 3}])))

    def test_missing_faults_key_rejected(self):
        plan = _plan()
        plan.pop("faults")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(_write_plan(td, plan))

    def test_omitted_package_name_defaults(self):
        plan = _plan()
        plan.pop("package_name")
        with tempfile.TemporaryDirectory() as td:
            loaded, _ = sm.load_and_validate_plan(_write_plan(td, plan))
        self.assertEqual(loaded["package_name"], sm.DEFAULT_PACKAGE_NAME)

    def test_explicitly_empty_package_name_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(
                    _write_plan(td, _plan({"package_name": ""})))

    def test_wifi_off_restart_beyond_plan_duration_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sm.PlanError):
                sm.load_and_validate_plan(
                    _write_plan(td, _plan({"duration_sec": 25})))


class CommandBuildTest(unittest.TestCase):
    SERIAL = "phone-a"
    PKG = "com.example.sensors_app"

    def test_each_action_builds_expected_argument_list(self):
        cases = {
            "wifi_off": [self.SERIAL, "shell", "svc", "wifi", "disable"],
            "wifi_on": [self.SERIAL, "shell", "svc", "wifi", "enable"],
            "screen_off": [self.SERIAL, "shell", "input", "keyevent", "223"],
            "screen_on": [self.SERIAL, "shell", "input", "keyevent", "224"],
            "force_stop": [self.SERIAL, "shell", "am", "force-stop", self.PKG],
            "launch": [self.SERIAL, "shell", "monkey", "-p", self.PKG,
                       "-c", "android.intent.category.LAUNCHER", "1"],
        }
        for action, expected in cases.items():
            with self.subTest(action=action):
                fault = sm.Fault(index=0, at_sec=1.0, serial=self.SERIAL, action=action)
                self.assertEqual(sm._build_command(fault, self.PKG, "adb"),
                                 ["adb", "-s"] + expected)

    def test_commands_are_argument_lists(self):
        for action in sm.ALL_ACTIONS:
            fault = sm.Fault(index=0, at_sec=1.0, serial="s", action=action)
            cmd = sm._build_command(fault, self.PKG)
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[1], "-s")
            self.assertEqual(cmd[2], "s")


class SerialCheckTest(unittest.TestCase):
    def test_serial_is_online(self):
        devices = [{"serial": "a", "state": "device"},
                   {"serial": "b", "state": "offline"}]
        self.assertTrue(sm._serial_is_online(devices, "a"))
        self.assertFalse(sm._serial_is_online(devices, "b"))
        self.assertFalse(sm._serial_is_online(devices, "missing"))


class FaultExecutionTest(unittest.TestCase):
    def test_screen_injection_permission_failure_is_explicitly_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            fault = sm.Fault(index=0, at_sec=1.0, serial="phone-a", action="screen_off")
            runner = _make_runner(
                td, [fault],
                last_devices=[{"serial": "phone-a", "state": "device"}],
            )
            try:
                with mock.patch(
                    "soak_monitor.subprocess.run",
                    return_value=FakeProc(
                        "", returncode=1,
                        stderr="java.lang.SecurityException: Injecting input events requires the caller to hold INJECT_EVENTS permission",
                    ),
                ):
                    runner._execute_fault(fault)
                self.assertTrue(fault.skipped)
                self.assertFalse(fault.executed)
                self.assertIn("INJECT_EVENTS permission", fault.skipped_reason)
                self.assertFalse(runner.degraded)
            finally:
                runner.close("test", 0)


class CliTest(unittest.TestCase):
    def _parse(self, *extra: str) -> argparse.Namespace:
        return sm._parse_args(["--plan", "plan.json", *extra])

    def test_stop_after_duration_canonical_default(self):
        self.assertTrue(self._parse().stop_after_planned)

    def test_stop_after_duration_explicit(self):
        self.assertTrue(self._parse("--stop-after-duration").stop_after_planned)

    def test_no_stop_after_duration(self):
        self.assertFalse(self._parse("--no-stop-after-duration").stop_after_planned)

    def test_stop_after_planned_alias(self):
        self.assertTrue(self._parse("--stop-after-planned").stop_after_planned)
        self.assertFalse(self._parse("--no-stop-after-planned").stop_after_planned)

    def test_help_exits_zero_and_lists_flag(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                self._parse("--help")
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--stop-after-duration", buf.getvalue())

    def test_plan_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                sm._parse_args([])
        self.assertEqual(cm.exception.code, 2)


class FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class AdbDevicesParseTest(unittest.TestCase):
    def test_devices_l_parses_serial_state_and_detail(self):
        with mock.patch("soak_monitor.subprocess.run", return_value=FakeProc(
                "List of devices attached\n"
                "emulator-5554 device product:sdk_gphone model:Pixel_4 "
                "device:goldfish transport_id:1\n"
                "R58M1234 offline\n")) as run:
            devices, err = sm._adb_devices("adb")
        self.assertIsNone(err)
        self.assertEqual(run.call_args.args[0], ["adb", "devices", "-l"])
        by_serial = {d["serial"]: d for d in devices}
        self.assertEqual(by_serial["emulator-5554"]["state"], "device")
        self.assertEqual(by_serial["emulator-5554"]["model"], "Pixel_4")
        self.assertEqual(by_serial["emulator-5554"]["transport_id"], "1")
        self.assertEqual(by_serial["R58M1234"]["state"], "offline")


def _make_runner(tmpdir: str, faults: list, last_devices: list | None = None,
                 **plan_extra: object):
    args = argparse.Namespace(
        backend="http://127.0.0.1:8000", adb="adb", dry_run=False,
        wait_for_recording=True, stop_after_planned=True, run_dir=tmpdir)
    plan = {"duration_sec": 60, "poll_sec": 5, "startup_timeout_sec": 120,
            "package_name": sm.DEFAULT_PACKAGE_NAME, **plan_extra}
    runner = sm.SoakRunner(args=args, plan=plan, faults=faults, run_dir=Path(tmpdir))
    if last_devices is not None:
        runner._last_devices = last_devices
    return runner


class PreflightTest(unittest.TestCase):
    def _faults(self) -> list:
        return [
            sm.Fault(index=0, at_sec=1.0, serial="phone-a", action="wifi_off"),
            sm.Fault(index=1, at_sec=2.0, serial="phone-b", action="screen_off"),
        ]

    def test_all_configured_serials_online_passes(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(td, self._faults(),
                                  last_devices=[{"serial": "phone-a", "state": "device"},
                                                {"serial": "phone-b", "state": "device"}])
            try:
                self.assertEqual(runner._preflight_serials(), [])
            finally:
                runner.close("test", 0)

    def test_missing_and_offline_serials_reported(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(
                td, self._faults(),
                last_devices=[{"serial": "phone-a", "state": "offline"}])
            try:
                self.assertEqual(runner._preflight_serials(), ["phone-a", "phone-b"])
            finally:
                runner.close("test", 0)

    def test_live_run_returns_nonzero_when_serial_missing(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(td, self._faults(),
                                  last_devices=[{"serial": "phone-a", "state": "device"}])
            reason, code = "test", 0
            try:
                with mock.patch("soak_monitor._adb_devices",
                                return_value=(
                                    [{"serial": "phone-a", "state": "device"}],
                                    None)):
                    reason, code = runner.run_live()
            finally:
                runner.close(reason, code)
            self.assertEqual(code, 1)
            self.assertEqual(reason, "preflight_serials_missing")
            self.assertTrue(runner.degraded)


class DegradedOnPollErrorTest(unittest.TestCase):
    def _runner(self, td: str) -> sm.SoakRunner:
        return _make_runner(td, [sm.Fault(index=0, at_sec=1.0, serial="a",
                                          action="wifi_on")])

    def test_health_poll_error_marks_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self._runner(td)
            try:
                with mock.patch("soak_monitor._http_get",
                                return_value=(None, None, "boom")):
                    ok, data = runner._poll_health()
                self.assertFalse(ok)
                self.assertIsNone(data)
                self.assertTrue(runner.degraded)
                self.assertEqual(runner.n_http_errors, 1)
            finally:
                runner.close("test", 0)

    def test_session_poll_error_marks_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self._runner(td)
            try:
                with mock.patch("soak_monitor._http_get",
                                return_value=(None, None, "boom")):
                    ok, data = runner._poll_session()
                self.assertFalse(ok)
                self.assertTrue(runner.degraded)
            finally:
                runner.close("test", 0)

    def test_adb_snapshot_error_marks_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self._runner(td)
            try:
                with mock.patch("soak_monitor._adb_devices",
                                return_value=([], "boom")):
                    runner._snapshot_adb()
                self.assertTrue(runner.degraded)
                self.assertEqual(runner._last_devices, [])
            finally:
                runner.close("test", 0)


if __name__ == "__main__":
    unittest.main()
