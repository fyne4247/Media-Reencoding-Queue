"""Operating-system differences for the re-encode queue (macOS, Linux, Windows)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl
    import select


def find_tool(name: str, configured: str | None = None) -> str | None:
    """Return a usable executable path: the configured one, else the PATH lookup."""
    if configured:
        found = shutil.which(configured)
        if found:
            return found
        if Path(configured).is_file():
            return configured
    return shutil.which(name)


def lock_file(handle) -> None:
    """Take a non-blocking exclusive lock; raise BlockingIOError if already held."""
    if IS_WINDOWS:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


_windows_line = ""


def poll_stop_line() -> str | None:
    """Return a completed line typed at the terminal, or None if nothing is ready.

    POSIX uses select() on stdin. Windows has no select() for consoles, so it
    collects keystrokes with msvcrt until Enter. Returns "" when stdin closes.
    """
    global _windows_line
    if IS_WINDOWS:
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                line, _windows_line = _windows_line, ""
                print()
                return line
            if char == "\b":
                _windows_line = _windows_line[:-1]
            else:
                _windows_line += char
                print(char, end="", flush=True)
        return None
    readable, _, _ = select.select([sys.stdin], [], [], 0)
    if readable:
        return sys.stdin.readline()
    return None


def install_signal_handlers() -> None:
    """Turn termination signals into KeyboardInterrupt so cleanup code runs."""
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, interrupt)


def move_to_trash(path: Path) -> None:
    """Send one file to the platform's recoverable trash; never unlink it."""
    if IS_MAC:
        script = (
            "on run argv\n"
            "  set sourceFile to POSIX file (item 1 of argv)\n"
            "  tell application \"Finder\" to delete sourceFile\n"
            "end run"
        )
        command = ["/usr/bin/osascript", "-e", script, str(path)]
    elif IS_WINDOWS:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("PowerShell was not found; cannot use the Recycle Bin")
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
            "$args[0], 'OnlyErrorDialogs', 'SendToRecycleBin')"
        )
        command = [powershell, "-NoProfile", "-Command", script, str(path)]
    else:
        gio = shutil.which("gio")
        trash_put = shutil.which("trash-put")
        if gio:
            command = [gio, "trash", str(path)]
        elif trash_put:
            command = [trash_put, str(path)]
        else:
            raise RuntimeError("no trash tool found; install 'gio' (glib) or 'trash-cli'")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode or path.exists():
        detail = completed.stderr.strip() or completed.stdout.strip() or "the trash tool did not move the file"
        raise RuntimeError(detail)


def trash_name() -> str:
    if IS_MAC:
        return "the macOS Trash"
    if IS_WINDOWS:
        return "the Recycle Bin"
    return "the desktop Trash"
