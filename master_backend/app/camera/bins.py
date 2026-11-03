"""Locating and spawning the ffmpeg child processes the camera service owns.

Resolution order (docs/rebuild/lanes/L5-cameras.md, hard rules):

1. environment ``IMU_FFMPEG`` / ``IMU_FFPROBE``;
2. ``vendor/ffmpeg/`` at the repository root (next to ``master_backend/``), which is
   where the packaged build (lane L8) puts ``ffmpeg.exe``;
3. ``PATH``.

Every ffmpeg child is spawned through :func:`spawn` so the Windows-only process-group
and Job Object handling lives in exactly one place. On Windows a child that survives a
backend crash would hold the webcam open forever (a webcam can only be opened by one
process at a time), so children are created with ``CREATE_NEW_PROCESS_GROUP`` and put
into a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: closing the last job
handle — which the OS does when the backend dies — terminates them.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent          # master_backend/app/camera
_REPO_ROOT = _PACKAGE_DIR.parents[1]                    # the checkout root
_VENDOR_DIR = _REPO_ROOT / "vendor" / "ffmpeg"

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


class FFmpegNotFound(RuntimeError):
    """No ffmpeg/ffprobe binary could be located."""


def _resolve(env_var: str, name: str) -> str:
    from_env = os.getenv(env_var, "").strip()
    if from_env:
        return from_env
    vendored = _VENDOR_DIR / f"{name}{_EXE_SUFFIX}"
    if vendored.exists():
        return str(vendored)
    found = shutil.which(name)
    if not found:
        raise FFmpegNotFound(
            f"{name} not found: set {env_var}, put it in {_VENDOR_DIR}, or add it to PATH"
        )
    return found


def ffmpeg_exe() -> str:
    """Absolute path (or bare name) of the ffmpeg binary to use."""
    return _resolve("IMU_FFMPEG", "ffmpeg")


def ffprobe_exe() -> str:
    """Absolute path (or bare name) of the ffprobe binary to use."""
    return _resolve("IMU_FFPROBE", "ffprobe")


# ---------------------------------------------------------------------------
# Windows Job Object (guarded; unit-tested by code review only — the lane spec
# says the Windows paths stay small because the production target cannot run here)
# ---------------------------------------------------------------------------
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_JobExtendedLimitInformation = 9

_job_lock = threading.Lock()
_job_handle: int | None = None


if sys.platform == "win32":  # pragma: no cover - exercised on the target laptop only

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _ensure_job() -> int | None:
        """Create (once) the job whose children die with the backend. Best effort."""
        global _job_handle
        with _job_lock:
            if _job_handle is not None:
                return _job_handle
            try:
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                handle = kernel32.CreateJobObjectW(None, None)
                if not handle:
                    logger.warning("CreateJobObjectW failed: %s", ctypes.GetLastError())
                    return None
                info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                ok = kernel32.SetInformationJobObject(
                    handle, _JobExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info),
                )
                if not ok:
                    logger.warning("SetInformationJobObject failed: %s",
                                   ctypes.GetLastError())
                _job_handle = handle
                return handle
            except Exception:  # never let job handling break capture
                logger.exception("could not create the ffmpeg Job Object")
                return None

    def _assign_to_job(proc: subprocess.Popen) -> None:
        job = _ensure_job()
        if not job:
            return
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # Popen._handle is the native PROCESS_ALL_ACCESS handle on Windows.
            # A process can only be in one job; on old Windows a nested assign
            # fails and the child simply keeps its normal lifetime.
            if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
                logger.warning("AssignProcessToJobObject failed for pid %s", proc.pid)
        except Exception:
            logger.exception("could not assign pid %s to the ffmpeg job", proc.pid)

else:

    def _assign_to_job(proc: subprocess.Popen) -> None:  # noqa: D103
        return None


def spawn(argv: list[str], **kwargs) -> subprocess.Popen:
    """Spawn one ffmpeg child: pipes on all three streams, Windows job membership.

    ``argv[0]`` is expected to be :func:`ffmpeg_exe` or :func:`ffprobe_exe`.
    Callers own the process lifetime; use :func:`request_quit` to shut down a
    streaming ffmpeg politely.
    """
    popen_kwargs: dict = dict(
        stdin=subprocess.PIPE,
        stdout=kwargs.pop("stdout", subprocess.PIPE),
        stderr=kwargs.pop("stderr", subprocess.PIPE),
        bufsize=kwargs.pop("bufsize", 1024 * 1024),
    )
    popen_kwargs.update(kwargs)
    if sys.platform == "win32":  # pragma: no cover
        popen_kwargs["creationflags"] = (
            _CREATE_NEW_PROCESS_GROUP | popen_kwargs.get("creationflags", 0)
        )
        # start_new_session would create a new process *group* on POSIX only; on
        # Windows the job object is what ties children to us.
    proc = subprocess.Popen(argv, **popen_kwargs)
    if sys.platform == "win32":  # pragma: no cover
        _assign_to_job(proc)
    return proc


def request_quit(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    """Shut a streaming ffmpeg down the polite way: ``q`` on stdin, then kill.

    Returns True when the process exited on its own within ``timeout`` seconds.
    """
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(b"q")
            proc.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    return False
