"""Portable process management (Stage 2G-a).

media_pipeline.kill_process_tree previously shelled out to the
Windows-only ``taskkill /F /T``; on Linux the fallback killed only the
direct child, leaking grandchildren (ffmpeg / yt-dlp subprocesses).
This module provides a platform-correct implementation:

- Windows: keep the existing taskkill behaviour for kill_process_tree,
  and use the Win32 API (OpenProcess + GetExitCodeProcess) for
  process_alive. The Windows branch is guarded by a sys.platform check
  so this module is import-safe on POSIX.
- POSIX: process_alive via os.kill(pid, 0); kill_process_tree sends
  SIGKILL to the child's process GROUP when the child is in a
  different group than us (killing our own group would kill us), and
  otherwise falls back to process.kill().

kill_process_tree never raises.
"""

from __future__ import annotations

import os
import subprocess
import sys

_IS_WINDOWS = sys.platform.startswith("win")


def process_alive(pid) -> bool:
    """Return True if a process with *pid* exists / is still running."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if _IS_WINDOWS:
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    # POSIX
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else.
        return True
    except OSError:
        return False
    return True


def kill_process_tree(process) -> None:
    """Terminate *process* and (where safe) its process tree/group.
    Never raises."""
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if not pid:
        return

    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            return
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
            return

    # POSIX
    try:
        import signal

        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid is not None and pgid != os.getpgrp():
            # Child leads its own group or sits in a different group
            # than us: killing that group cannot kill us.
            try:
                os.killpg(pgid, signal.SIGKILL)
                return
            except OSError:
                pass
        # Same process group as us (or group unknown): kill only the
        # direct child — killpg here would terminate ourselves.
        try:
            process.kill()
        except Exception:
            pass
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
