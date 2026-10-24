#!/usr/bin/env python3
"""System assessment: are Python, FFmpeg and the configured folders ready?"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import encoders
import platform_support
import settings

OK, WARN, FAIL = "[ ok ]", "[warn]", "[FAIL]"
MIN_PYTHON = (3, 9)


def tool_version(path: str) -> str:
    completed = subprocess.run([path, "-version"], text=True, capture_output=True, check=False)
    first = (completed.stdout or completed.stderr).splitlines()
    return first[0] if first else "version unknown"


def same_device(path: Path, other: Path) -> bool:
    def existing(candidate: Path) -> Path:
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate
    return existing(path).stat().st_dev == existing(other).stat().st_dev


def assess(config_path: Path | None = None, quiet: bool = False) -> bool:
    """Print a checklist. Returns False if anything required is missing."""
    ready = True

    def report(status: str, text: str) -> None:
        nonlocal ready
        if status == FAIL:
            ready = False
        if not quiet or status != OK:
            print(f"  {status} {text}")

    print(f"System check ({platform.system()} {platform.machine()})")
    version = sys.version_info
    if version[:2] >= MIN_PYTHON:
        report(OK, f"Python {version.major}.{version.minor}.{version.micro}")
    else:
        report(FAIL, f"Python {version.major}.{version.minor} is too old; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")

    config: dict | None = None
    if config_path and config_path.exists():
        try:
            config = settings.load_ini(config_path)
        except settings.SettingsError as exc:
            report(FAIL, str(exc))
    configured = lambda key: (config or {}).get(key)

    ffmpeg = platform_support.find_tool("ffmpeg", configured("ffmpeg"))
    ffprobe = platform_support.find_tool("ffprobe", configured("ffprobe"))
    hint = {"darwin": "brew install ffmpeg", "win32": "winget install Gyan.FFmpeg"}.get(
        sys.platform, "install ffmpeg with your package manager (apt, dnf, pacman...)")
    for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        if path:
            report(OK, f"{name}: {path}  ({tool_version(path)[:60]})")
        else:
            report(FAIL, f"{name} not found. Try: {hint}")
    mpv = platform_support.find_tool("mpv", configured("mpv"))
    if mpv:
        report(OK, f"mpv: {mpv}")
    else:
        report(WARN, "mpv not found (optional: lets you preview a spot with source decode errors)")

    found: list[str] = []
    if ffmpeg:
        found = encoders.available_encoders(ffmpeg)
        if found:
            report(OK, "HEVC encoders in this FFmpeg: " + ", ".join(found))
        else:
            report(FAIL, "this FFmpeg build has no supported HEVC encoder (libx265, nvenc, qsv, amf, videotoolbox)")

    if config:
        wanted = config["video"]["encoder"]
        if found and wanted not in found:
            report(FAIL, f"configured encoder {wanted} is not available in this FFmpeg build")
        elif found:
            report(OK, f"configured encoder: {wanted}")
        base = config_path.parent
        resolve = lambda value: (Path(value).expanduser() if Path(value).expanduser().is_absolute()
                                 else base / value).resolve()
        source = resolve(config["source_root"])
        if not source.is_dir():
            report(FAIL, f"library folder not found: {source}")
        else:
            report(OK, f"library folder: {source}")
            if not os.access(source, os.W_OK):
                report(FAIL, "library folder is not writable (needed to place verified encodes)")
            for label in ("staging_root", "originals_root"):
                target = resolve(config[label])
                try:
                    if not same_device(target, source):
                        report(FAIL, f"{label} {target} is on a different drive than the library; "
                                     "promotion needs the same filesystem")
                    else:
                        report(OK, f"{label}: {target}")
                except OSError as exc:
                    report(FAIL, f"{label}: {exc}")
            probe = resolve(config["staging_root"])
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            free_gib = shutil.disk_usage(probe).free / 1024 ** 3
            reserve = float(config["minimum_free_gib"])
            status = OK if free_gib > reserve else FAIL
            report(status, f"free space on the library drive: {free_gib:.1f} GiB (reserve {reserve:g} GiB)")
    elif config_path:
        report(WARN, f"no config yet at {config_path}; the setup survey will create it")
    print()
    return ready


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.ini")
    args = parser.parse_args()
    return 0 if assess(args.config) else 1


if __name__ == "__main__":
    raise SystemExit(main())
