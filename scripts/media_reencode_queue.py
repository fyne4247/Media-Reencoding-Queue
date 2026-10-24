#!/usr/bin/env python3
"""Resumable, inventory-backed FFmpeg re-encode queue.

Encodes are written beneath a separate staging root and verified before they
are either retained there or promoted into the library according to config.
Promotion moves the source into the configured volume-level Originals root;
it never deletes the source.
"""

from __future__ import annotations

import argparse
import copy
import csv
import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import encoders
import settings
import platform_support
from reencode_report import write_report


STOP_AFTER_CURRENT = False
STOP_INPUT_ENABLED = False
QUEUE_LOCK_FD = None

# These are per-file outcomes that need a human choice.  They are deliberately
# kept out of the normal queue once recorded: unattended runs must not exit
# early, nor should they spend a later night retrying a deterministic problem.
DEFERRED_DECISION_STATUSES = frozenset({
    "encode_failed",
    "existing_invalid",
    "promotion_failed",
    "processing_failed",
    "source_decode_deferred",
    "source_decode_failed",
    "verification_failed",
})

# Outcomes that leave a finished HEVC file in the library.  Only "verified"
# adds to the encoding-speed history: the others took no encode time this run,
# so counting their video duration would inflate the speed estimate.
COMPLETED_STATUSES = frozenset({
    "verified",
    "existing_verified",
    "verified_on_recheck",
    "verified_with_manual_override",
})


def partial_marker(config: dict[str, Any]) -> Path:
    return Path(config['state_json']).with_suffix('.partial.json')


def cleanup_partial(config: dict[str, Any]) -> None:
    """Remove only the unfinished output explicitly recorded by this queue."""
    marker = partial_marker(config)
    data = load_json(marker, None)
    if data is None:
        return
    partial = Path(data['partial'])
    staging = Path(config['staging_root']).resolve()
    if partial.is_symlink() or staging not in partial.resolve().parents or not partial.name.endswith('.partial.mkv'):
        raise RuntimeError(f'refusing unsafe partial cleanup: {partial}')
    if partial.exists():
        partial.unlink()
        print(f'Removed unfinished encode: {partial}', flush=True)
    marker.unlink(missing_ok=True)


def retain_verification_failed_output(partial: Path, staged: Path) -> Path:
    """Keep a completed, rejected encode without ever overwriting an earlier one."""
    if not partial.exists() or partial.is_symlink():
        raise RuntimeError(f"cannot retain missing or unsafe verification output: {partial}")
    candidate = staged.with_name(f"{staged.stem}.verification-failed{staged.suffix}")
    index = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = staged.with_name(f"{staged.stem}.verification-failed.{index}{staged.suffix}")
        index += 1
    partial.replace(candidate)
    return candidate


def poll_stop_input() -> None:
    """Read line-based commands only while a subprocess owns the foreground work."""
    global STOP_AFTER_CURRENT, STOP_INPUT_ENABLED
    if not STOP_INPUT_ENABLED or not sys.stdin.isatty():
        return
    command = platform_support.poll_stop_line()
    if command is not None:
        if not command:
            STOP_INPUT_ENABLED = False
        elif command.strip().lower() in ('s', 'stop'):
            STOP_AFTER_CURRENT = True
            print('\nStop requested: finishing the current file and verification, then saving the run report.', flush=True)
        elif command.strip():
            print('\nType S then Return to stop after the current file.', flush=True)


VIDEO_EXTENSIONS = {
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}

DEFAULT_CODECS = ["h264", "avc1", "mpeg4", "mpeg2video", "mpeg1video", "vc1", "vp8"]
# Set from the configured encoder's profile in main(); empty means software decode.
HARDWARE_DECODE_ARGS: list[str] = []
INVENTORY_FIELDS = [
    "path",
    "relative_path",
    "size_bytes",
    "mtime_ns",
    "duration_seconds",
    "overall_bitrate_bps",
    "video_codec",
    "video_profile",
    "video_width",
    "video_height",
    "video_fps",
    "video_bitrate_bps",
    "video_pixel_format",
    "video_stream_count",
    "audio_stream_count",
    "audio_codecs",
    "audio_channels",
    "subtitle_stream_count",
    "subtitle_codecs",
    "chapter_count",
    "ffprobe_error",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def human_bytes(value: int | float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(command, text=True, stdout=subprocess.PIPE if capture else None,
                          pass_fds=(QUEUE_LOCK_FD,) if QUEUE_LOCK_FD is not None else (),
                          stderr=subprocess.PIPE if capture else None) as process:
        try:
            deadline = time.monotonic() + 60 if Path(command[0]).name == 'ffprobe' else None
            while True:
                poll_stop_input()
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, 60)
                timeout = min(0.5, remaining) if remaining is not None else 0.5
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def probe_file(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,bit_rate,format_name:chapter=start_time,end_time:"
            "stream=index,codec_type,codec_name,profile,width,height,pix_fmt,avg_frame_rate,"
            "r_frame_rate,bit_rate,bits_per_raw_sample,channels,channel_layout,sample_rate,"
            "color_range,color_space,color_transfer,color_primaries:stream_tags:stream_disposition",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"ffprobe exited {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def frame_rate(stream: dict[str, Any]) -> float:
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = str(raw).split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0.0


def streams(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [stream for stream in data.get("streams", []) if stream.get("codec_type") == kind]


def main_video_streams(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stream
        for stream in streams(data, "video")
        if not stream.get("disposition", {}).get("attached_pic")
    ]


def compact_probe(path: Path, root: Path, data: dict[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    format_data = data.get("format", {})
    videos = streams(data, "video")
    mains = main_video_streams(data)
    audios = streams(data, "audio")
    subtitles = streams(data, "subtitle")
    video = mains[0] if mains else {}
    return {
        "path": str(path),
        "relative_path": str(path.relative_to(root)),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration_seconds": as_float(format_data.get("duration")),
        "overall_bitrate_bps": as_int(format_data.get("bit_rate")),
        "video_codec": video.get("codec_name", ""),
        "video_profile": video.get("profile", ""),
        "video_width": as_int(video.get("width")),
        "video_height": as_int(video.get("height")),
        "video_fps": round(frame_rate(video), 3),
        "video_bitrate_bps": as_int(video.get("bit_rate")),
        "video_pixel_format": video.get("pix_fmt", ""),
        "video_stream_count": len(videos),
        "main_video_stream_count": len(mains),
        "audio_stream_count": len(audios),
        "audio_codecs": ";".join(str(item.get("codec_name", "")) for item in audios),
        "audio_channels": ";".join(str(item.get("channels", "")) for item in audios),
        "subtitle_stream_count": len(subtitles),
        "subtitle_codecs": ";".join(str(item.get("codec_name", "")) for item in subtitles),
        "chapter_count": len(data.get("chapters", [])),
        "ffprobe_error": "",
    }


def fingerprint(record: dict[str, Any]) -> str:
    return f"{as_int(record.get('size_bytes'))}:{as_int(record.get('mtime_ns'))}"


def is_generated_name(path: Path) -> bool:
    lowered = path.name.lower()
    markers = (".partial.", "[original]", "[hevc]", "[compact hevc]", "[av1]")
    return any(marker in lowered for marker in markers)


def matches_any(relative: Path, patterns: Iterable[str]) -> bool:
    unix_path = relative.as_posix()
    return any(
        fnmatch.fnmatch(unix_path, pattern)
        or fnmatch.fnmatch(relative.name, pattern)
        for pattern in patterns
    )


def discover(config: dict[str, Any]) -> list[Path]:
    root = Path(config["source_root"]).resolve()
    staging = Path(config["staging_root"]).resolve()
    patterns = config.get("exclude_globs", [])
    extensions = {item.lower() for item in config.get("extensions", VIDEO_EXTENSIONS)}
    skipped = {item.casefold() for item in config.get("skip_folders", ["Originals"])}
    found: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
            and not name.startswith('.')
            and name.casefold() not in skipped
            and not name.endswith('.trickplay')
            and (current_path / name).resolve() != staging
        ]
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink() or path.suffix.lower() not in extensions or is_generated_name(path):
                continue
            relative = path.relative_to(root)
            if matches_any(relative, patterns):
                continue
            found.append(path.resolve())
    return sorted(found, key=lambda item: str(item).casefold())


def write_inventory_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=INVENTORY_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(records)
    temporary.replace(path)


def rescan(config: dict[str, Any], ffprobe: str) -> list[dict[str, Any]]:
    root = Path(config["source_root"]).resolve()
    json_path = Path(config["inventory_json"])
    csv_path = Path(config["inventory_csv"])
    old_data = load_json(json_path, {"files": []})
    cached = {item.get("path"): item for item in old_data.get("files", [])}
    paths = discover(config)
    records: list[dict[str, Any]] = []
    changed = 0

    print(f"Rescanning {len(paths)} source video files...", flush=True)
    for index, path in enumerate(paths, start=1):
        stat = path.stat()
        old = cached.get(str(path))
        if (
            old
            and not old.get('ffprobe_error')
            and as_int(old.get("size_bytes")) == stat.st_size
            and as_int(old.get("mtime_ns")) == stat.st_mtime_ns
        ):
            records.append(old)
            continue
        changed += 1
        try:
            data = probe_file(path, ffprobe)
            records.append(compact_probe(path, root, data))
        except (OSError, RuntimeError) as exc:
            records.append(
                {
                    "path": str(path),
                    "relative_path": str(path.relative_to(root)),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "ffprobe_error": str(exc),
                }
            )
        if changed % 25 == 0:
            print(f"  probed {changed} new or changed files ({index}/{len(paths)})", flush=True)

    records.sort(key=lambda item: str(item.get("relative_path", "")).casefold())
    generated = {"generated_at": utc_now(), "root": str(root), "files": records}
    atomic_json(json_path, generated)
    write_inventory_csv(csv_path, records)
    print(f"Inventory updated: {len(records)} files; {changed} needed probing.", flush=True)
    return records


def load_inventory(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(config["inventory_json"])
    data = load_json(path, None)
    if not data or not isinstance(data.get("files"), list):
        raise RuntimeError(f"no usable inventory at {path}; run without --no-rescan first")
    root = Path(config["source_root"])
    stored_root = Path(str(data.get("root", root)))
    return [relocate_record(record, stored_root, root) for record in data["files"]]


def relocate_value(value: Any, old_root: Path, new_root: Path) -> Any:
    """Move a persisted absolute path from an older volume mount to this one."""
    if not isinstance(value, str):
        return value
    old_text = str(old_root)
    if value == old_text:
        return str(new_root)
    prefix = old_text.rstrip("/") + "/"
    if value.startswith(prefix):
        return str(new_root / value[len(prefix):])
    return value


def relocate_record(record: dict[str, Any], old_root: Path, new_root: Path) -> dict[str, Any]:
    relocated = dict(record)
    relocated["path"] = relocate_value(record.get("path"), old_root, new_root)
    return relocated


def relocate_tree(value: Any, mappings: list[tuple[Path, Path]]) -> Any:
    """Relocate persisted paths anywhere in a JSON-compatible state tree."""
    if isinstance(value, dict):
        relocated: dict[Any, Any] = {}
        for key, item in value.items():
            relocated_key = key
            for old_root, new_root in mappings:
                relocated_key = relocate_value(relocated_key, old_root, new_root)
            relocated[relocated_key] = relocate_tree(item, mappings)
        return relocated
    if isinstance(value, list):
        return [relocate_tree(item, mappings) for item in value]
    for old_root, new_root in mappings:
        value = relocate_value(value, old_root, new_root)
    return value


def state_template(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "name": config["name"],
        "source_root": config["source_root"],
        "staging_root": config["staging_root"],
        "updated_at": utc_now(),
        "jobs": {},
        "history": {
            "verified_files": 0,
            "encoded_source_seconds": 0.0,
            "encoding_wall_seconds": 0.0,
        },
    }


def load_state(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config["state_json"])
    state = load_json(path, state_template(config))
    old_source = Path(str(state.get("source_root", config["source_root"])))
    old_staging = Path(str(state.get("staging_root", config["staging_root"])))
    mappings = [(old_source, Path(config["source_root"])), (old_staging, Path(config["staging_root"]))]
    if config.get("originals_root"):
        mappings.append((old_source.parent / "Originals", Path(config["originals_root"])))
    state = relocate_tree(state, mappings)
    state["source_root"] = config["source_root"]
    state["staging_root"] = config["staging_root"]
    state.setdefault("jobs", {})
    state.setdefault("history", state_template(config)["history"])
    return state


def save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(Path(config["state_json"]), state)


def deferred_template(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "name": config.get("name", "re-encode queue"),
        "source_root": config.get("source_root", ""),
        "updated_at": utc_now(),
        "decisions": {},
    }


def deferred_key(record: dict[str, Any]) -> str:
    """A stable key that survives a future volume-mount rename."""
    return Path(str(record.get("relative_path", record["path"]))).as_posix()


def deferred_path(config: dict[str, Any]) -> Path:
    configured = config.get("deferred_decisions_json")
    if configured:
        return Path(configured)
    state_path = Path(config["state_json"])
    return state_path.with_name(f"{state_path.stem}_deferred_decisions.json")


def load_deferred(config: dict[str, Any]) -> dict[str, Any]:
    path = deferred_path(config)
    data = load_json(path, deferred_template(config))
    if not isinstance(data, dict):
        data = deferred_template(config)
    data.setdefault("decisions", {})
    data["name"] = config.get("name", data.get("name", "re-encode queue"))
    data["source_root"] = config.get("source_root", data.get("source_root", ""))
    return data


def save_deferred(config: dict[str, Any], deferred: dict[str, Any]) -> None:
    deferred["updated_at"] = utc_now()
    atomic_json(deferred_path(config), deferred)


def defer_record(
    record: dict[str, Any],
    result: "EncodeResult",
    output: Path,
    deferred: dict[str, Any],
) -> None:
    """Persist enough context for a later choice without touching the source."""
    decisions = deferred["decisions"]
    key = deferred_key(record)
    previous = decisions.get(key, {})
    decisions[key] = {
        "source": record["path"],
        "relative_path": key,
        "source_fingerprint": fingerprint(record),
        "output": str(output),
        "status": result.status,
        "first_deferred_at": previous.get("first_deferred_at", utc_now()),
        "last_deferred_at": utc_now(),
        "attempts": as_int(previous.get("attempts")) + 1,
        "verification": result.verification or {"ok": False},
        "retry_requested": False,
    }
    retained = (result.verification or {}).get("retained_staging_output")
    if retained:
        decisions[key]["retained_staging_output"] = retained


def release_deferred_for_retry(deferred: dict[str, Any]) -> tuple[int, int]:
    """Let deferred items without a held output into the next run, once.

    For use after the cause of a failure has been fixed (a tool update, a
    queue change, a repaired source).  The decision stays recorded: a success
    clears it, and another failure defers the file again with the flag reset.
    Held outputs are skipped because re-checking them is cheaper than a
    re-encode and every run already does that.
    """
    released = 0
    held = 0
    for item in deferred.get("decisions", {}).values():
        if retained_output_text(item):
            held += 1
            continue
        if not item.get("retry_requested"):
            item["retry_requested"] = True
            item["retry_requested_at"] = utc_now()
        released += 1
    return released, held


def prune_deferred(records: list[dict[str, Any]], deferred: dict[str, Any]) -> bool:
    """Forget decisions whose source changed or disappeared after a review."""
    current = {deferred_key(record): fingerprint(record) for record in records}
    decisions = deferred["decisions"]
    stale = [
        key for key, item in decisions.items()
        if key not in current or item.get("source_fingerprint") != current[key]
        # Disk capacity is a run-wide stop condition, not a decision that
        # should hide the file after free space later becomes available.
        or item.get("status") == "insufficient_space"
    ]
    for key in stale:
        decisions.pop(key, None)
    return bool(stale)


def deferred_entries(deferred: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        deferred.get("decisions", {}).values(),
        key=lambda item: (str(item.get("last_deferred_at", "")), str(item.get("relative_path", ""))),
        reverse=True,
    )


def print_deferred_decisions(config: dict[str, Any], deferred: dict[str, Any]) -> None:
    entries = deferred_entries(deferred)
    print(f"\nDeferred decisions: {len(entries)}")
    if not entries:
        print("  None. Unattended runs have no files awaiting review.")
        return
    print("  These sources remain untouched and are excluded from normal runs until reviewed.")
    print("  [held] = a finished encode is waiting in staging and can be re-checked or accepted.\n")
    for index, item in enumerate(entries, start=1):
        verification = item.get("verification") or {}
        details = verification.get("errors") or verification.get("source_decode_errors") or []
        detail = str(details[0]) if details else "no additional diagnostic was captured"
        relative = Path(str(item.get("relative_path", "(unknown)")))
        held = " [held]" if retained_output_text(item) else ""
        if item.get("retry_requested"):
            held += " [retry next run]"
        print(f"  {index:3}. {library_folder({'relative_path': str(relative)})} / {relative.name}{held}")
        print(f"       {str(item.get('status', 'unknown')).replace('_', ' ')}"
              f" (attempt {as_int(item.get('attempts'))}): {detail}")


def move_deferred_source_to_trash(record: dict[str, Any], config: dict[str, Any]) -> None:
    """Move one manually selected library source to the system trash, never unlink it."""
    source = Path(record["path"])
    root = Path(config["source_root"]).resolve()
    if source.is_symlink() or not source.exists() or root not in source.resolve().parents:
        raise RuntimeError(f"refusing to move unsafe deferred source to Trash: {source}")
    platform_support.move_to_trash(source)


def eligible(record: dict[str, Any], config: dict[str, Any]) -> tuple[bool, str]:
    if record.get("ffprobe_error"):
        return False, "probe error"
    codec = str(record.get("video_codec", "")).lower()
    allowed = {item.lower() for item in config.get("include_codecs", DEFAULT_CODECS)}
    if codec not in allowed:
        return False, f"codec {codec or 'unknown'} is not selected"
    if record.get('video_pixel_format') not in {'yuv420p', 'yuvj420p', 'nv12', 'yuv420p10le', 'p010le'}:
        return False, 'pixel format requires manual review to preserve bit depth and chroma'
    if as_int(record.get("main_video_stream_count"), 1) != 1:
        return False, "does not have exactly one main video stream"
    if as_int(record.get("video_stream_count"), 1) != 1:
        return False, "has attached or additional video streams; manual review required"
    if as_float(record.get("duration_seconds")) <= 0:
        return False, "duration is missing"
    overall = as_int(record.get("overall_bitrate_bps"))
    minimum_predicted = float(config.get("minimum_predicted_savings_percent", 0))
    if overall and minimum_predicted:
        rates = choose_rate(record, config)
        audio_count = as_int(record.get("audio_stream_count"))
        audio_allowance = audio_count * as_int(
            config.get("video", {}).get("unknown_audio_bitrate_bps", 192_000)
        )
        predicted_savings = (1 - (rates["target_bps"] + audio_allowance) / overall) * 100
        if predicted_savings < minimum_predicted:
            return False, f"already too compact for a useful encode (estimated {predicted_savings:.1f}% savings)"
    return True, "eligible"


def output_path(record: dict[str, Any], config: dict[str, Any]) -> Path:
    """Configured output path: library destination when promoting, staging otherwise."""
    if not config.get("promote_verified", False):
        return staging_output_path(record, config)
    source = Path(record["path"])
    return source.with_suffix(".mkv")


def staging_output_path(record: dict[str, Any], config: dict[str, Any]) -> Path:
    relative = Path(record["relative_path"])
    if relative.is_absolute() or '..' in relative.parts:
        raise RuntimeError('unsafe relative inventory path')
    return (Path(config["staging_root"]) / relative).with_suffix(".mkv")


def promote_verified(source: Path, staged: Path, final: Path, config: dict[str, Any]) -> dict[str, str]:
    """Move a verified encode into place while retaining the original."""
    if not config.get("promote_verified", False):
        staged.replace(final)
        return {"promotion": "staged_only", "output": str(final)}

    originals_root = Path(config["originals_root"])
    source_root = Path(config["source_root"])
    original_destination = originals_root / source.relative_to(source_root)
    originals = original_destination.parent
    if originals_root.resolve() not in original_destination.resolve().parents:
        raise RuntimeError('archive path escapes Originals root through a symlink')
    if original_destination.exists() or original_destination.is_symlink():
        raise RuntimeError(f"original destination already exists: {original_destination}")
    if final != source and (final.exists() or final.is_symlink()):
        raise RuntimeError(f"final library destination already exists: {final}")

    originals.mkdir(parents=True, exist_ok=True)
    if config.get('results_jsonl'):
        append_jsonl(Path(config['results_jsonl']), {
            'timestamp': utc_now(), 'status': 'promotion_planned', 'source': str(source),
            'original': str(original_destination), 'staged': str(staged), 'output': str(final),
        })
    source.replace(original_destination)
    try:
        staged.replace(final)
    except BaseException:
        # Restore the source if the final move fails. The verified staged file
        # remains available for inspection/retry.
        original_destination.replace(source)
        raise
    return {
        "promotion": "promoted",
        "output": str(final),
        "original": str(original_destination),
        "nfo": "same-basename sidecar retained beside promoted file",
    }


def library_folder(record: dict[str, Any]) -> str:
    """Return the Jellyfin/Plex-style item folder used for library-level ranking.

    The library is expected to be organized as Movies/<title>/... and TV/<series>/....  Keeping
    the first two path components means a whole series is ranked together,
    including all of its season folders and extras.
    """
    relative = Path(str(record.get("relative_path", "")))
    parts = relative.parts
    if len(parts) >= 3 and parts[0] == 'Movies':
        for index in range(len(parts) - 2, 0, -1):
            if re.search(r'\(\d{4}\)', parts[index]):
                return Path(*parts[:index + 1]).as_posix()
    if len(parts) >= 2 and parts[0] in {"Movies", "TV"}:
        return Path(parts[0], parts[1]).as_posix()
    if parts:
        return parts[0]
    return "(unknown folder)"


def choose_rate(record: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    video_config = config.get("video", {})
    ratio = float(video_config.get("source_bitrate_ratio", 0.70))
    height = as_int(record.get("video_height"))
    overall = as_int(record.get("overall_bitrate_bps"))
    source_video = as_int(record.get("video_bitrate_bps"))
    if not source_video:
        assumed_audio = as_int(record.get("audio_stream_count")) * as_int(
            video_config.get("unknown_audio_bitrate_bps", 192_000)
        )
        source_video = max(0, overall - assumed_audio)
    if not source_video:
        duration = as_float(record.get("duration_seconds"))
        source_video = int(as_int(record.get("size_bytes")) * 8 / duration * 0.9)

    tiers = video_config.get(
        "bitrate_tiers",
        [
            {"max_height": 480, "minimum_bps": 600_000, "maximum_bps": 1_200_000},
            {"max_height": 720, "minimum_bps": 950_000, "maximum_bps": 1_800_000},
            {"max_height": 1080, "minimum_bps": 2_500_000, "maximum_bps": 6_000_000},
            {"max_height": 2160, "minimum_bps": 10_000_000, "maximum_bps": 22_000_000},
        ],
    )
    tier = next((item for item in tiers if height <= as_int(item.get("max_height"))), tiers[-1])
    target = int(source_video * ratio)
    target = max(as_int(tier.get("minimum_bps")), min(target, as_int(tier.get("maximum_bps"))))
    maximum = int(target * float(video_config.get("maxrate_multiplier", 1.5)))
    buffer_size = int(target * float(video_config.get("buffer_multiplier", 2.0)))
    return {"source_video_bps": source_video, "target_bps": target, "maxrate_bps": maximum, "bufsize_bps": buffer_size}


def estimated_savings(record: dict[str, Any], config: dict[str, Any]) -> dict[str, float | int]:
    """Estimate the result of the configured encode without creating media."""
    source_size = as_int(record.get("size_bytes"))
    duration = as_float(record.get("duration_seconds"))
    rates = choose_rate(record, config)
    audio_bps = as_int(record.get("audio_stream_count")) * as_int(
        config.get("video", {}).get("unknown_audio_bitrate_bps", 192_000)
    )
    # Add a small muxing/bitrate cushion. This is intentionally conservative;
    # the verified result after encoding is the source of truth.
    predicted_size = int(duration * (rates["target_bps"] + audio_bps) / 8 * 1.05) if duration else source_size
    saved = max(0, source_size - predicted_size)
    percent = (saved / source_size * 100) if source_size else 0.0
    return {
        "predicted_size_bytes": predicted_size,
        "estimated_saved_bytes": saved,
        "estimated_saved_percent": percent,
        "target_bps": rates["target_bps"],
    }


def folder_ranking(
    records: list[dict[str, Any]], config: dict[str, Any], state: dict[str, Any],
    deferred: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        ok, reason = eligible(record, config)
        if not ok:
            continue
        if deferred and deferred_key(record) in deferred.get("decisions", {}):
            continue
        output = output_path(record, config)
        job = state.get("jobs", {}).get(record["path"], {})
        if (
            job.get("status") in COMPLETED_STATUSES
            and job.get("source_fingerprint") == fingerprint(record)
            and output.exists()
            and job.get("output_size_bytes") == output.stat().st_size
        ):
            continue
        folder = library_folder(record)
        group = groups.setdefault(folder, {"folder": folder, "records": [], "source_bytes": 0, "saved_bytes": 0})
        estimate = estimated_savings(record, config)
        group["records"].append(record)
        group["source_bytes"] += as_int(record.get("size_bytes"))
        group["saved_bytes"] += as_int(estimate["estimated_saved_bytes"])

    ranked = []
    for group in groups.values():
        group["file_count"] = len(group["records"])
        group["estimated_saved_percent"] = (
            group["saved_bytes"] / group["source_bytes"] * 100 if group["source_bytes"] else 0.0
        )
        group["codecs"] = sorted({str(item.get("video_codec", "?")) for item in group["records"]})
        group["pixel_formats"] = sorted({str(item.get("video_pixel_format", "?")) for item in group["records"]})
        ranked.append(group)
    ranked.sort(key=lambda item: (-item["saved_bytes"], item["folder"].casefold()))
    for index, group in enumerate(ranked, start=1):
        group["rank"] = index
    return ranked


def print_folder_ranking(ranked: list[dict[str, Any]], limit: int = 10) -> None:
    print("\nHighest-priority folders by estimated space saved")
    print("(eligible pending files only; estimates are not guarantees)\n")
    if not ranked:
        print("  No eligible pending folders found.")
        return
    for group in ranked[:limit]:
        print(
            f"  {group['rank']:2}. {group['folder']} — "
            f"save about {human_bytes(group['saved_bytes'])} "
            f"({group['estimated_saved_percent']:.1f}%) across {group['file_count']} file(s) "
            f"[{', '.join(group['codecs'])}; {', '.join(group['pixel_formats'])}]"
        )


def stream_tag(stream: dict[str, Any], name: str) -> str:
    """Look up a stream tag case-insensitively.

    Matroska files authored by mkvmerge-style tools can carry ``LANGUAGE`` as
    an uppercase tag, which FFmpeg rewrites as the lowercase track language.
    The value is the same; only the key's spelling differs.
    """
    for key, value in stream.get("tags", {}).items():
        if str(key).lower() == name:
            return str(value)
    return ""


def stream_tags(stream: dict[str, Any]) -> tuple[str, str]:
    return (stream_tag(stream, "language"), stream_tag(stream, "title"))


UNSET_LANGUAGES = frozenset({"", "und"})


def normalize_added_languages(
    source_data: dict[str, Any], output_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Ignore a language the muxer filled in for an untagged source track.

    Returns a copy of the output probe with those languages set back to the
    source's value, plus a warning for each one.  A track that already had a
    real language must still match exactly.
    """
    normalized = copy.deepcopy(output_data)
    warnings: list[str] = []
    for kind in ("audio", "subtitle"):
        pairs = zip(streams(source_data, kind), streams(normalized, kind))
        for index, (source, output) in enumerate(pairs, start=1):
            source_language = stream_tag(source, "language")
            output_language = stream_tag(output, "language")
            if source_language.lower() in UNSET_LANGUAGES and output_language != source_language:
                tags = {key: value for key, value in output.get("tags", {}).items() if str(key).lower() != "language"}
                if source_language:
                    tags["language"] = source_language
                output["tags"] = tags
                warnings.append(
                    f"{kind} stream {index} had no language tag ({source_language or 'blank'}); "
                    f"output is labeled {output_language!r}"
                )
    return normalized, warnings


def stream_disposition(stream: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((str(key), as_int(value)) for key, value in stream.get("disposition", {}).items() if value))


def audio_signature(data: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            item.get("codec_name"),
            as_int(item.get("channels")),
            as_int(item.get("sample_rate")),
            item.get("channel_layout", ""),
            stream_tags(item),
            stream_disposition(item),
        )
        for item in streams(data, "audio")
    ]


def audio_stream_details(stream: dict[str, Any]) -> dict[str, Any]:
    language, title = stream_tags(stream)
    disposition = ", ".join(key for key, _value in stream_disposition(stream)) or "none"
    return {
        "codec": str(stream.get("codec_name", "")),
        "channels": as_int(stream.get("channels")),
        "sample rate": as_int(stream.get("sample_rate")),
        "channel layout": str(stream.get("channel_layout", "")),
        "language": language,
        "title": title,
        "disposition": disposition,
    }


def audio_differences(source_data: dict[str, Any], output_data: dict[str, Any]) -> list[str]:
    """Describe the exact copied-audio mismatch in a reportable form."""
    source_audio = streams(source_data, "audio")
    output_audio = streams(output_data, "audio")
    if len(source_audio) != len(output_audio):
        return [f"audio stream count changed: {len(source_audio)} -> {len(output_audio)}"]
    differences: list[str] = []
    for index, (source, output) in enumerate(zip(source_audio, output_audio), start=1):
        source_fields = audio_stream_details(source)
        output_fields = audio_stream_details(output)
        for field, source_value in source_fields.items():
            output_value = output_fields[field]
            if source_value != output_value:
                differences.append(
                    f"audio stream {index} {field} changed: {source_value!r} -> {output_value!r}"
                )
    return differences or ["audio stream signature changed for an unrecognized reason"]


def subtitle_signature(data: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (item.get("codec_name"), stream_tags(item), stream_disposition(item))
        for item in streams(data, "subtitle")
    ]


def subtitle_content_signature(data: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Track identity and user-facing subtitle labels, excluding dispositions."""
    return [
        (item.get("codec_name"), stream_tags(item))
        for item in streams(data, "subtitle")
    ]


def subtitle_compatibility_signature(data: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Treat MOV text and Matroska SRT as the same text-subtitle content class."""
    def compatible_codec(codec: Any) -> Any:
        return "text" if codec in {"mov_text", "subrip"} else codec
    return [
        (compatible_codec(item.get("codec_name")), stream_tags(item))
        for item in streams(data, "subtitle")
    ]


def color_signature(stream: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        stream.get(key)
        for key in ("color_range", "color_space", "color_transfer", "color_primaries")
    )


def spot_decode(path: Path, duration: float, ffmpeg: str) -> list[str]:
    failures: list[str] = []
    for fraction in (0.1, 0.5, 0.9):
        start = max(0.0, duration * fraction - 1.0)
        completed = run(
            [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                *HARDWARE_DECODE_ARGS,
                "-ss",
                f"{start:.3f}",
                "-i",
                str(path),
                "-t",
                "2",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-fps_mode:v",
                "passthrough",
                "-enc_time_base:v",
                "demux",
                "-f",
                "null",
                "-",
            ],
            capture=True,
        )
        if completed.returncode or completed.stderr.strip():
            failures.append(f"decode at {fraction:.0%}: {completed.stderr.strip()}")
    return failures


def source_decode_check(path: Path, ffmpeg: str, decode_args: list[str] | None = None) -> dict[str, Any]:
    """Decode the source video before spending time on a re-encode.

    This catches malformed containers and video reference errors before an
    output is produced. It records the last progress timestamp available when
    an error occurs so the caller can show the user approximately where to
    inspect it.
    """
    completed = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            *(HARDWARE_DECODE_ARGS if decode_args is None else decode_args),
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-fps_mode:v",
            "passthrough",
            "-enc_time_base:v",
            "demux",
            "-f",
            "null",
            "-progress",
            "pipe:2",
            "-",
        ],
        capture=True,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    progress_seconds = 0.0
    first_error_seconds: float | None = None
    progress_keys = {
        "frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time_us",
        "out_time_ms", "out_time", "dup_frames", "drop_frames", "speed", "progress", "elapsed",
    }
    error_lines = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in {"out_time", "time"}:
            match = re.search(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", line.split("=", 1)[-1])
            if match:
                hours = int(match.group(1) or 0)
                minutes = int(match.group(2))
                seconds = float(match.group(3))
                progress_seconds = hours * 3600 + minutes * 60 + seconds
        elif line and key not in progress_keys and not re.fullmatch(r'stream_\d+_\d+_q', key):
            if first_error_seconds is None:
                first_error_seconds = progress_seconds
            error_lines.append(line)
    if completed.returncode and not error_lines:
        error_lines = [f"ffmpeg exited {completed.returncode}"]
    return {"errors": error_lines[:20], "seconds": first_error_seconds if first_error_seconds is not None else 0.0}


def checked_source(record: dict[str, Any], config: dict[str, Any], ffmpeg: str) -> dict[str, Any]:
    """Reuse a completed clean decode only while the source fingerprint matches."""
    cache_path = Path(config['state_json']).with_name('source_decode_cache.json') if config.get('state_json') else None
    cache = load_json(cache_path, {}) if cache_path else {}
    if cache:
        # A cache has no root metadata of its own. Keep entries whose paths
        # still exist and discard stale mount-point keys; the next check will
        # repopulate those entries under the current volume path.
        cache = {key: value for key, value in cache.items() if Path(key).exists()}
    key = record['path']
    signature = fingerprint(record)
    cached = cache.get(key) or {}
    if cached.get('fingerprint') == signature and cached.get('check_version') == 1:
        print('  Previous full decode passed; source size and modification time are unchanged.', flush=True)
        return {'errors': [], 'seconds': 0.0, 'software_decode': bool(cached.get('software_decode'))}
    started = time.monotonic()
    result = source_decode_check(Path(key), ffmpeg)
    if result['errors'] and HARDWARE_DECODE_ARGS:
        # A hardware decoder can reject a healthy stream. Software decoding is the
        # reference: if it is clean, the file is fine, but this file must then be
        # encoded with software decode too, since the hardware path garbles it.
        print('  Hardware decode reported errors; re-checking the whole video with software decode...', flush=True)
        result = source_decode_check(Path(key), ffmpeg, decode_args=[])
        result['software_decode'] = not result['errors']
    print(f'  Full video check finished in {human_time(time.monotonic() - started)}.', flush=True)
    stat = Path(key).stat()
    if not result['errors'] and signature == f'{stat.st_size}:{stat.st_mtime_ns}':
        cache[key] = {'fingerprint': signature, 'check_version': 1}
        if result.get('software_decode'):
            cache[key]['software_decode'] = True
        if cache_path:
            atomic_json(cache_path, cache)
    return result


def verify(source: Path, output: Path, config: dict[str, Any], ffprobe: str, ffmpeg: str) -> dict[str, Any]:
    source_data = probe_file(source, ffprobe)
    output_data = probe_file(output, ffprobe)
    source_format = source_data.get("format", {})
    output_format = output_data.get("format", {})
    source_duration = as_float(source_format.get("duration"))
    output_duration = as_float(output_format.get("duration"))
    source_videos = main_video_streams(source_data)
    output_videos = main_video_streams(output_data)
    errors: list[str] = []
    warnings: list[str] = []
    allow_automatic_metadata_normalization = bool(config.get("allow_automatic_metadata_normalization", False))
    if allow_automatic_metadata_normalization:
        output_data, language_warnings = normalize_added_languages(source_data, output_data)
        warnings.extend(language_warnings)

    duration_tolerance = max(1.5, source_duration * 0.001)
    if abs(source_duration - output_duration) > duration_tolerance:
        errors.append(f"duration differs by {abs(source_duration - output_duration):.3f}s")
    if len(source_videos) != 1 or len(output_videos) != 1:
        errors.append(f"main video stream count changed: {len(source_videos)} -> {len(output_videos)}")
    else:
        source_video, output_video = source_videos[0], output_videos[0]
        if (source_video.get("width"), source_video.get("height")) != (
            output_video.get("width"),
            output_video.get("height"),
        ):
            errors.append("video dimensions changed")
        if output_video.get("codec_name") != "hevc":
            errors.append("output video is not HEVC")
        source_is_10bit = "10" in str(source_video.get("pix_fmt", ""))
        output_is_10bit = "10" in str(output_video.get("pix_fmt", ""))
        if source_is_10bit and not output_is_10bit:
            errors.append("10-bit source was reduced to 8-bit")
        source_color = color_signature(source_video)
        output_color = color_signature(output_video)
        if source_color != output_color:
            # VideoToolbox writes a normal TV-range label for sources that did
            # not declare any color fields. That is not a pixel conversion.
            if allow_automatic_metadata_normalization and all(value is None for value in source_color):
                warnings.append("video color metadata was added to an otherwise untagged source")
            else:
                errors.append("video color metadata changed")
    if audio_signature(source_data) != audio_signature(output_data):
        errors.extend(audio_differences(source_data, output_data))
    if subtitle_compatibility_signature(source_data) != subtitle_compatibility_signature(output_data):
        errors.append("subtitle streams or subtitle metadata changed")
    elif subtitle_content_signature(source_data) != subtitle_content_signature(output_data):
        warnings.append("MOV text subtitles were converted to SRT for Matroska compatibility")
    elif subtitle_signature(source_data) != subtitle_signature(output_data):
        # Subtitle tracks themselves still match. Stream-copy remuxing can
        # normalize Matroska disposition flags, so record rather than reject it.
        if allow_automatic_metadata_normalization:
            warnings.append("subtitle disposition flags were normalized")
        else:
            errors.append("subtitle streams or subtitle metadata changed")
    if [(s.get('codec_name'), s.get('tags', {})) for s in streams(source_data, 'attachment')] != [
        (s.get('codec_name'), s.get('tags', {})) for s in streams(output_data, 'attachment')
    ]:
        errors.append('attachments or attachment metadata changed')
    if len(source_data.get("chapters", [])) != len(output_data.get("chapters", [])):
        errors.append("chapter count changed")

    source_size = as_int(source_format.get("size"), source.stat().st_size)
    output_size = as_int(output_format.get("size"), output.stat().st_size)
    saved_percent = (1 - output_size / source_size) * 100 if source_size else 0.0
    minimum_savings = float(config.get("minimum_savings_percent", 5.0))
    if output_size >= source_size:
        errors.append("output is not smaller than source")
    elif saved_percent < minimum_savings:
        errors.append(f"output saves only {saved_percent:.2f}%, below the {minimum_savings:.2f}% minimum")
    errors.extend(spot_decode(output, output_duration, ffmpeg))
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "source_duration_seconds": source_duration,
        "output_duration_seconds": output_duration,
        "duration_difference_seconds": abs(source_duration - output_duration),
        "source_size_bytes": source_size,
        "output_size_bytes": output_size,
        "saved_bytes": source_size - output_size,
        "saved_percent": round(saved_percent, 2),
    }


def capacity_bytes(record: dict[str, Any], config: dict[str, Any]) -> int:
    rates = choose_rate(record, config)
    # Reserve copied non-video bytes as well as a bitrate overshoot allowance.
    duration = as_float(record.get('duration_seconds'))
    nonvideo = max(as_int(record.get('audio_stream_count')) * 256_000,
                   as_int(record.get('overall_bitrate_bps')) - rates['source_video_bps'])
    return int(duration * (rates['target_bps'] + nonvideo) / 8 * 1.25)


def free_space_ok(record: dict[str, Any], rates: dict[str, int], config: dict[str, Any]) -> tuple[bool, str]:
    staging = Path(config["staging_root"])
    staging.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(staging).free
    predicted = capacity_bytes(record, config)
    reserve = int(float(config.get("minimum_free_gib", 10)) * 1024**3)
    required = predicted + reserve
    if free < required:
        return False, f"need about {human_bytes(required)} free including reserve; only {human_bytes(free)} is available"
    return True, f"{human_bytes(free)} free"


def print_capacity_report(queue: list[dict[str, Any]], config: dict[str, Any], requested: int) -> int:
    """Show a conservative batch capacity estimate and return how many fit."""
    staging = Path(config["staging_root"])
    staging.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(staging).free
    reserve = int(float(config.get("minimum_free_gib", 10)) * 1024**3)
    available = max(0, free - reserve)
    selected = queue[:requested]
    estimates = [capacity_bytes(item, config) for item in selected]
    cumulative = 0
    fit_count = 0
    for estimate in estimates:
        if cumulative + estimate > available:
            break
        cumulative += estimate
        fit_count += 1

    print("\nDisk-space preflight")
    print(f"  free now:              {human_bytes(free)}")
    print(f"  safety reserve:        {human_bytes(reserve)}")
    print(f"  usable for this batch: {human_bytes(available)}")
    print(
        f"  requested:             {len(selected)} file(s); "
        f"estimated output growth: {human_bytes(sum(estimates))}"
    )
    print(
        f"  conservative capacity: {fit_count} of {len(selected)} requested file(s) "
        f"({human_bytes(cumulative)} estimated)"
    )
    return fit_count


@dataclass
class EncodeResult:
    ok: bool
    status: str
    verification: dict[str, Any] | None
    wall_seconds: float


def encode_one(record, config, ffmpeg, ffprobe, *args, **kwargs) -> EncodeResult:
    started = time.monotonic()
    try:
        return _encode_one(record, config, ffmpeg, ffprobe, *args, **kwargs)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        # A bad path, a changed source, a collision, or a probe/mux exception
        # belongs to this file.  Record and defer it rather than abandoning an
        # unattended queue that can still make progress elsewhere.
        return EncodeResult(False, "processing_failed", {"ok": False, "errors": [str(exc)]}, time.monotonic() - started)
    finally:
        cleanup_partial(config)


def _encode_one(
    record: dict[str, Any],
    config: dict[str, Any],
    ffmpeg: str,
    ffprobe: str,
    prompt_on_source_errors: bool = True,
    deadline: float | None = None,
    allow_source_errors: bool = False,
) -> EncodeResult:
    source = Path(record["path"])
    output = output_path(record, config)
    staged = staging_output_path(record, config)
    partial = staged.with_name(f"{staged.stem}.partial.mkv")
    if partial.is_symlink() or Path(config['staging_root']).resolve() not in partial.resolve().parents:
        raise RuntimeError('unsafe staging path')
    staged.parent.mkdir(parents=True, exist_ok=True)
    root = Path(config['source_root']).resolve()
    if source.is_symlink() or root not in source.resolve().parents:
        raise RuntimeError(f'unsafe source path: {source}')
    stat = source.stat()
    if fingerprint(record) != f'{stat.st_size}:{stat.st_mtime_ns}':
        raise RuntimeError(f'source changed since inventory: {source}; refresh first')
    if config.get('promote_verified'):
        archive = Path(config['originals_root']) / source.relative_to(root)
        if archive.exists() or archive.is_symlink() or (output != source and output.exists()):
            raise RuntimeError(f'promotion destination collision for {source}')
    enough_space, message = free_space_ok(record, choose_rate(record, config), config)
    if not enough_space:
        return EncodeResult(False, 'insufficient_space', {'ok': False, 'errors': [message]}, 0.0)

    # For an already-MKV source, the promoted destination is intentionally
    # the same path as the source. It must not be treated as an existing
    # encoded output; the source is moved aside only after staged verification.
    if output != source and output.exists():
        verification = verify(source, output, config, ffprobe, ffmpeg)
        return EncodeResult(verification["ok"], "existing_verified" if verification["ok"] else "existing_invalid", verification, 0.0)

    print(f"Checking source integrity: {record['relative_path']}", flush=True)
    source_check = checked_source(record, config, ffmpeg)
    if deadline is not None and time.monotonic() >= deadline:
        return EncodeResult(False, 'time_limit', source_check, 0.0)
    source_errors = source_check["errors"]
    source_seconds = as_float(source_check.get("seconds"))
    source_warning: dict[str, Any] = {}
    software_decode = bool(source_check.get("software_decode"))
    if software_decode:
        print("  Hardware decode failed on this source but software decode is clean; "
              "this file will be encoded with software decode.", flush=True)
    if source_errors:
        timestamp = human_time(source_seconds)
        print("\nFFmpeg found source decode/container errors.", file=sys.stderr, flush=True)
        print(f"  Approximate location: {timestamp}", file=sys.stderr, flush=True)
        for detail in source_errors[-8:]:
            print(f"  {detail}", file=sys.stderr, flush=True)
        source_warning = {
            "source_decode_errors": source_errors,
            "source_error_seconds": source_seconds,
            "proceeded_after_source_errors": False,
        }
        if allow_source_errors:
            print("Proceeding despite source decode errors by manual deferred decision.", file=sys.stderr, flush=True)
            source_warning["proceeded_after_source_errors"] = True
        elif not prompt_on_source_errors or not sys.stdin.isatty():
            print("Deferring this source without a decision prompt.", file=sys.stderr, flush=True)
            return EncodeResult(False, "source_decode_deferred", {"ok": False, **source_warning}, 0.0)
        else:
            inspect = input("Open this source in mpv about 2 seconds before that point? [y/N] ").strip().lower()
            if inspect in {"y", "yes"}:
                mpv = platform_support.find_tool("mpv", config.get("mpv"))
                if mpv and Path(mpv).exists():
                    start = max(0.0, source_seconds - 2.0)
                    print(f"Opening mpv at {human_time(start)}; close mpv to return here.", flush=True)
                    subprocess.run([mpv, f"--start={start:.3f}", str(source)], check=False)
                else:
                    print("mpv was not found; continuing to the decision prompt.", file=sys.stderr, flush=True)
            proceed = input("Proceed with this encode anyway? [y/N] ").strip().lower()
            if proceed not in {"y", "yes"}:
                return EncodeResult(False, "source_decode_failed", {"ok": False, **source_warning}, 0.0)
            source_warning["proceeded_after_source_errors"] = True

    rates = choose_rate(record, config)
    enough_space, space_message = free_space_ok(record, rates, config)
    if not enough_space:
        print(f"Insufficient space: {space_message}", file=sys.stderr, flush=True)
        return EncodeResult(False, "insufficient_space", None, 0.0)

    ten_bit = "10" in str(record.get("video_pixel_format", ""))
    video_config = config.get("video", {})
    encoder_name = str(video_config.get("encoder", encoders.DEFAULT_ENCODER))
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats",
        "-stats_period",
        "15",
        "-nostdin",
        *([] if software_decode else HARDWARE_DECODE_ARGS),
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map",
        "0:t?",
        "-c:t",
        "copy",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c:v",
        encoder_name,
    ]
    command.extend(encoders.encoder_args(encoder_name, ten_bit, hw_frames=not software_decode))
    command.extend(
        [
            "-b:v",
            str(rates["target_bps"]),
            "-maxrate",
            str(rates["maxrate_bps"]),
            "-bufsize",
            str(rates["bufsize_bps"]),
            "-tag:v",
            "hvc1",
            "-c:a",
            "copy",
            "-c:s",
            "copy",
        ]
    )
    # Matroska can retain the image-based DVD subtitle track, but it cannot
    # mux QuickTime's mov_text codec. Convert only those text tracks to SRT;
    # all other subtitle streams remain copied unchanged.
    for index, codec in enumerate(str(record.get("subtitle_codecs", "")).split(";")):
        if codec == "mov_text":
            command.extend([f"-c:s:{index}", "srt"])
    command.append(str(partial))

    print(f"\nEncoding: {record['relative_path']}", flush=True)
    print(
        f"  source {human_bytes(as_int(record.get('size_bytes')))}, {human_time(as_float(record.get('duration_seconds')))}; "
        f"video target {rates['target_bps'] / 1_000_000:.2f} Mb/s; {space_message}",
        flush=True,
    )
    started = time.monotonic()
    atomic_json(partial_marker(config), {'partial': str(partial)})
    try:
        completed = run(command, capture=True)
    except KeyboardInterrupt:
        return EncodeResult(False, "interrupted", None, time.monotonic() - started)
    wall_seconds = time.monotonic() - started
    if completed.returncode:
        error_lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        errors = error_lines[-20:] or [f"ffmpeg exited {completed.returncode} without diagnostic text"]
        return EncodeResult(False, "encode_failed", {"ok": False, "errors": errors}, wall_seconds)

    verification = verify(source, partial, config, ffprobe, ffmpeg)
    stat = source.stat()
    if fingerprint(record) != f'{stat.st_size}:{stat.st_mtime_ns}':
        retained = retain_verification_failed_output(partial, staged)
        return EncodeResult(False, "processing_failed", {
            "ok": False,
            "errors": ["source changed during encoding; held output was not promoted"],
            "retained_staging_output": str(retained),
        }, wall_seconds)
    if source_warning:
        verification.update(source_warning)
    if software_decode:
        verification["warnings"] = [*verification.get("warnings", []),
                                    "Hardware decode failed on this source; it passed a full software decode "
                                    "and was encoded with software decode."]
    if not verification["ok"]:
        retained = retain_verification_failed_output(partial, staged)
        verification["retained_staging_output"] = str(retained)
        print(
            f"Verification rejected the completed output; retained for review: {retained}",
            file=sys.stderr,
            flush=True,
        )
        return EncodeResult(False, "verification_failed", verification, wall_seconds)
    # From here on this is a verified output, even if promotion fails.
    partial_marker(config).unlink(missing_ok=True)
    try:
        promotion = promote_verified(source, partial, output, config)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return EncodeResult(
            False,
            "promotion_failed",
            {**verification, "ok": False, "errors": [str(exc)]},
            wall_seconds,
        )
    verification["promotion"] = promotion
    print(
        f"Verified: saved {human_bytes(verification['saved_bytes'])} ({verification['saved_percent']:.2f}%).",
        flush=True,
    )
    return EncodeResult(True, "verified", verification, wall_seconds)


def queue_rows(
    records: list[dict[str, Any]], config: dict[str, Any], state: dict[str, Any],
    *, order: str | None = None, deferred: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    counts = {
        "total": len(records), "eligible": 0, "complete": 0, "pending": 0,
        "pending_seconds": 0.0, "skipped": 0, "failed": 0, "deferred": 0,
    }
    completed_outputs = {
        str(job.get("output")): job
        for job in state.get("jobs", {}).values()
        if job.get("status") in COMPLETED_STATUSES and job.get("output")
    }
    for record in records:
        record = dict(record)
        completed_job = state.get("jobs", {}).get(record["path"], {})
        if completed_job.get("status") not in COMPLETED_STATUSES:
            completed_job = completed_outputs.get(record["path"], {})
        if (
            completed_job.get("status") in COMPLETED_STATUSES
            and as_int(completed_job.get("output_size_bytes")) == as_int(record.get("size_bytes"))
        ):
            counts["complete"] += 1
            continue
        ok, reason = eligible(record, config)
        record["eligibility_reason"] = reason
        if not ok:
            counts["skipped"] += 1
            continue
        counts["eligible"] += 1
        decision = (deferred or {}).get("decisions", {}).get(deferred_key(record), {})
        if decision:
            if decision.get("retry_requested"):
                record["retry_requested"] = True
            else:
                counts["deferred"] += 1
                continue
        job = state.get("jobs", {}).get(record["path"], {})
        if job.get("status") in {
            "encode_failed",
            "verification_failed",
            "existing_invalid",
            "insufficient_space",
            "source_decode_failed",
            "source_decode_deferred",
        }:
            counts["failed"] += 1
        counts["pending"] += 1
        counts["pending_seconds"] += as_float(record.get("duration_seconds"))
        rows.append(record)

    order = order if order is not None else config.get("order", "path")
    if order == 'file_savings':
        rows.sort(key=lambda item: (
            -as_int(estimated_savings(item, config)['estimated_saved_bytes']),
            str(item.get('relative_path', '')).casefold(),
        ))
    elif order == 'folder_savings':
        totals: dict[str, int] = {}
        for item in rows:
            folder = library_folder(item)
            totals[folder] = totals.get(folder, 0) + as_int(estimated_savings(item, config)['estimated_saved_bytes'])
        rows.sort(key=lambda item: (-totals[library_folder(item)], library_folder(item), item['relative_path']))
    elif order == "largest_first":
        rows.sort(key=lambda item: (-as_int(item.get("size_bytes")), str(item.get("relative_path", "")).casefold()))
    elif order == "shortest_first":
        rows.sort(key=lambda item: (as_float(item.get("duration_seconds")), str(item.get("relative_path", "")).casefold()))
    else:
        rows.sort(key=lambda item: str(item.get("relative_path", "")).casefold())
    return rows, counts


def print_status(config: dict[str, Any], counts: dict[str, int], state: dict[str, Any]) -> None:
    history = state.get("history", {})
    saved = sum(
        as_int(job.get("saved_bytes"))
        for job in state.get("jobs", {}).values()
        if job.get("status") in COMPLETED_STATUSES
    )
    print(f"\nQueue: {config['name']}")
    print(f"  source:  {config['source_root']}")
    print(f"  staging: {config['staging_root']}")
    print(
        f"  total {counts['total']} | pending {counts['pending']} | "
        f"complete {counts['complete']} | skipped {counts['skipped']} | deferred {counts['deferred']}"
    )
    if counts["failed"]:
        print(f"  pending items with an earlier failure: {counts['failed']}")
    if counts.get("deferred"):
        print(f"  awaiting a manual deferred decision: {counts['deferred']}")
    if saved:
        print(f"  verified staged savings: {human_bytes(saved)}")
    wall = as_float(history.get("encoding_wall_seconds"))
    media = as_float(history.get("encoded_source_seconds"))
    verified_files = as_int(history.get("verified_files"))
    if verified_files or media or wall:
        print(f"  hours of video reencoded: {media / 3600:.1f}")
        print(f"  hours spent encoding: {wall / 3600:.1f}")
    if wall > 0:
        speed = media / wall
        print(f"  average encoding speed: {speed:.2f}x real time")
        print(
            f"  estimated time left: {human_time(as_float(counts['pending_seconds']) / speed)} "
            f"({as_float(counts['pending_seconds']) / 3600:.1f} hours of pending video)"
        )


def validate_config(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    required = [
        "name",
        "source_root",
        "staging_root",
        "inventory_json",
        "inventory_csv",
        "state_json",
        "results_jsonl",
    ]
    if config.get("promote_verified") and not config.get("originals_root"):
        raise RuntimeError(f"promote_verified requires originals_root in {config_path}")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RuntimeError(f"missing config keys in {config_path}: {', '.join(missing)}")
    config_dir = config_path.parent

    def resolve_config_path(value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = config_dir / path
        return path.resolve()

    source = resolve_config_path(config["source_root"])
    staging = resolve_config_path(config["staging_root"])
    if not source.is_dir():
        raise RuntimeError(f"source root does not exist: {source}")
    if source == staging or source in staging.parents or staging in source.parents:
        raise RuntimeError("source and staging roots must be separate, non-nested folders")
    config = dict(config)
    config["source_root"] = str(source)
    config["staging_root"] = str(staging)
    if config.get("originals_root"):
        config["originals_root"] = str(resolve_config_path(config["originals_root"]))
        archive = Path(config['originals_root'])
        for other in (source, staging):
            if archive == other or archive in other.parents or other in archive.parents:
                raise RuntimeError('originals root must be separate from source and staging')
        for destination in (archive, staging):
            existing = destination
            while not existing.exists():
                existing = existing.parent
            if existing.stat().st_dev != source.stat().st_dev:
                raise RuntimeError('promotion requires source, staging and Originals on the same filesystem')
    for key in ("inventory_json", "inventory_csv", "state_json", "results_jsonl"):
        config[key] = str(resolve_config_path(config[key]))
    deferred_path = config.get("deferred_decisions_json")
    if deferred_path:
        config["deferred_decisions_json"] = str(resolve_config_path(deferred_path))
    else:
        state_path = Path(config["state_json"])
        config["deferred_decisions_json"] = str(state_path.with_name(f"{state_path.stem}_deferred_decisions.json"))
    return config


def lock_queue(config: dict[str, Any]):
    lock_path = Path(config["state_json"]).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        platform_support.lock_file(handle)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f"this queue is already running (lock: {lock_path})")
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={utc_now()}\n")
    handle.flush()
    return handle


def result_entry(record: dict[str, Any], result: EncodeResult, output: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "timestamp": utc_now(),
        "source": record["path"],
        "source_fingerprint": fingerprint(record),
        "output": str(output),
        "status": result.status,
        "wall_seconds": round(result.wall_seconds, 3),
    }
    if result.verification:
        entry["verification"] = result.verification
        entry["output_size_bytes"] = as_int(result.verification.get("output_size_bytes"))
        entry["saved_bytes"] = as_int(result.verification.get("saved_bytes"))
    return entry


def save_result(config: dict[str, Any], state: dict[str, Any], record: dict[str, Any], result: EncodeResult) -> None:
    entry = result_entry(record, result, output_path(record, config))
    append_jsonl(Path(config["results_jsonl"]), entry)
    job = dict(entry)
    job.pop("verification", None)
    state["jobs"][record["path"]] = job
    if result.ok and result.status == "verified":
        history = state["history"]
        history["verified_files"] = as_int(history.get("verified_files")) + 1
        history["encoded_source_seconds"] = as_float(history.get("encoded_source_seconds")) + as_float(
            record.get("duration_seconds")
        )
        history["encoding_wall_seconds"] = as_float(history.get("encoding_wall_seconds")) + result.wall_seconds
    save_state(config, state)


def retained_output_text(item: dict[str, Any]) -> str:
    return str(item.get("retained_staging_output") or (item.get("verification") or {}).get("retained_staging_output") or "")


def usable_retained_output(
    record: dict[str, Any], item: dict[str, Any], config: dict[str, Any],
) -> tuple[Path, str]:
    """Return the held output and an empty string, or a reason it cannot be used."""
    retained_text = retained_output_text(item)
    retained = Path(retained_text)
    staging = Path(config["staging_root"]).resolve()
    if (
        not retained_text or retained.is_symlink() or not retained.exists()
        or staging not in retained.resolve().parents
    ):
        return retained, "the retained verification output is missing or outside the staging root"
    source = Path(record["path"])
    stat = source.stat()
    if fingerprint(record) != f"{stat.st_size}:{stat.st_mtime_ns}":
        return retained, f"source changed since inventory: {source}; refresh first"
    return retained, ""


def recheck_retained_output(
    record: dict[str, Any], item: dict[str, Any], config: dict[str, Any], ffprobe: str, ffmpeg: str,
) -> EncodeResult:
    """Verify a held output again under the current rules; promote it only if it passes.

    This is how a rejection caused by an over-strict check is cleared without
    spending another encode: the same output is probed and spot-decoded again.
    """
    source = Path(record["path"])
    started = time.monotonic()
    retained, problem = usable_retained_output(record, item, config)
    if problem:
        return EncodeResult(False, "verification_failed", {
            "ok": False, "errors": [problem], "retained_staging_output": retained_output_text(item),
        }, 0.0)
    verification = verify(source, retained, config, ffprobe, ffmpeg)
    if not verification["ok"]:
        verification["retained_staging_output"] = str(retained)
        return EncodeResult(False, "verification_failed", verification, time.monotonic() - started)
    try:
        verification["promotion"] = promote_verified(source, retained, output_path(record, config), config)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        verification["retained_staging_output"] = str(retained)
        return EncodeResult(False, "promotion_failed", {**verification, "ok": False, "errors": [str(exc)]}, 0.0)
    return EncodeResult(True, "verified_on_recheck", verification, time.monotonic() - started)


def recheck_deferred_outputs(
    records: list[dict[str, Any]], config: dict[str, Any], state: dict[str, Any],
    deferred: dict[str, Any], ffprobe: str, ffmpeg: str, *, only_key: str | None = None,
) -> tuple[int, int]:
    """Re-verify every held verification-failed output; promote those that now pass."""
    by_key = {deferred_key(record): record for record in records}
    candidates = [
        item for item in deferred_entries(deferred)
        if item.get("status") == "verification_failed" and retained_output_text(item)
        and (only_key is None or item.get("relative_path") == only_key)
    ]
    promoted = 0
    still_deferred = 0
    if candidates:
        print(f"Re-checking {len(candidates)} held output(s) against the current verification rules...", flush=True)
    for item in candidates:
        record = by_key.get(str(item.get("relative_path")))
        if record is None or fingerprint(record) != item.get("source_fingerprint"):
            still_deferred += 1
            continue
        result = recheck_retained_output(record, item, config, ffprobe, ffmpeg)
        save_result(config, state, record, result)
        key = deferred_key(record)
        if result.ok:
            deferred["decisions"].pop(key, None)
            promoted += 1
            saved = as_int(result.verification.get("saved_bytes")) if result.verification else 0
            print(f"  promoted: {record['relative_path']} (saved {human_bytes(saved)})", flush=True)
        else:
            defer_record(record, result, output_path(record, config), deferred)
            still_deferred += 1
            details = (result.verification or {}).get("errors") or ["no diagnostic"]
            print(f"  still deferred: {record['relative_path']}: {details[0]}", flush=True)
        save_deferred(config, deferred)
    if candidates:
        print(f"Re-check finished: {promoted} promoted, {still_deferred} still deferred.", flush=True)
    return promoted, still_deferred


def accept_retained_verification_output(
    record: dict[str, Any], item: dict[str, Any], config: dict[str, Any],
) -> EncodeResult:
    """Promote a held completed output after the user explicitly accepts its mismatch."""
    source = Path(record["path"])
    retained, problem = usable_retained_output(record, item, config)
    if problem:
        return EncodeResult(False, "promotion_failed", {"ok": False, "errors": [problem]}, 0.0)
    verification = dict(item.get("verification") or {})
    rejected_errors = list(verification.get("errors") or [])
    verification["errors"] = []
    verification["ok"] = True
    verification["manual_verification_override"] = {
        "accepted_at": utc_now(),
        "rejected_errors": rejected_errors,
    }
    warnings = list(verification.get("warnings") or [])
    warnings.extend(f"Manually accepted despite verification mismatch: {error}" for error in rejected_errors)
    verification["warnings"] = warnings
    try:
        verification["promotion"] = promote_verified(source, retained, output_path(record, config), config)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        verification["ok"] = False
        verification["errors"] = [str(exc)]
        return EncodeResult(False, "promotion_failed", verification, 0.0)
    return EncodeResult(True, "verified_with_manual_override", verification, 0.0)


def adopt_previous_failures(
    records: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any], deferred: dict[str, Any],
) -> int:
    """One-time migration of failed rows from the old resume state into the inbox."""
    latest: dict[str, dict[str, Any]] = {}
    results = Path(config["results_jsonl"])
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("source"):
                latest[entry["source"]] = entry

    adopted = 0
    for record in records:
        if deferred_key(record) in deferred["decisions"]:
            continue
        job = state.get("jobs", {}).get(record["path"], {})
        status = job.get("status")
        if status not in DEFERRED_DECISION_STATUSES or job.get("source_fingerprint") != fingerprint(record):
            continue
        prior = latest.get(record["path"], {})
        verification = prior.get("verification") or {
            "ok": False,
            "errors": [f"Previous {str(status).replace('_', ' ')}; no detailed diagnostic was retained."],
        }
        result = EncodeResult(False, status, verification, as_float(job.get("wall_seconds")))
        defer_record(record, result, output_path(record, config), deferred)
        adopted += 1
    return adopted


def resolve_deferred_record(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    config: dict[str, Any],
    state: dict[str, Any],
    deferred: dict[str, Any],
    ffmpeg: str,
    ffprobe: str,
) -> int:
    entries = deferred_entries(deferred)
    if not 1 <= args.deferred_index <= len(entries):
        raise RuntimeError(f"deferred decision number is out of range (1-{len(entries)})")
    item = entries[args.deferred_index - 1]
    record = next((record for record in records if deferred_key(record) == item["relative_path"]), None)
    if record is None or fingerprint(record) != item.get("source_fingerprint"):
        raise RuntimeError("that deferred source changed or disappeared; refresh the deferred list")

    key = deferred_key(record)
    action = args.deferred_action
    if action == "trash":
        if not args.confirm_trash:
            raise RuntimeError("moving a source to the trash requires --confirm-trash")
        move_deferred_source_to_trash(record, config)
        result = EncodeResult(False, "trashed", {
            "ok": False,
            "errors": [f"Source moved to {platform_support.trash_name()} by manual deferred decision."],
        }, 0.0)
        save_result(config, state, record, result)
        deferred["decisions"].pop(key, None)
        save_deferred(config, deferred)
        print(f"Moved to Trash: {record['relative_path']}")
        return 0

    if action == "recheck":
        if item.get("status") != "verification_failed" or not retained_output_text(item):
            raise RuntimeError("re-check needs a held output from a verification failure; use retry instead")
        recheck_deferred_outputs(records, config, state, deferred, ffprobe, ffmpeg, only_key=key)
        return 0

    # A verification failure is deterministic: encoding the same source with
    # the same settings produces the same rejection.  Without a held output to
    # accept, "encode anyway" has nothing to override, so say so instead of
    # spending another encode only to defer the file again.
    if action == "encode-anyway" and item.get("status") == "verification_failed" and not retained_output_text(item):
        raise RuntimeError(
            "this verification failure has no held output to accept; use retry to encode it again"
        )

    # "Encode anyway" means accept a completed verification-rejected output
    # when one exists.  It is not a second nine-minute encode, and promotion
    # still uses the ordinary collision checks and source-to-Originals move.
    if action == "encode-anyway" and item.get("status") == "verification_failed":
        result = accept_retained_verification_output(record, item, config)
        save_result(config, state, record, result)
        if result.ok:
            deferred["decisions"].pop(key, None)
            save_deferred(config, deferred)
            print(
                f"Accepted and promoted held output: {record['relative_path']} "
                "(the source was retained under Originals)."
            )
        else:
            defer_record(record, result, output_path(record, config), deferred)
            save_deferred(config, deferred)
            print(f"Still deferred: {record['relative_path']} (promotion failed).", file=sys.stderr)
        return 0

    # A direct retry is an explicit decision, so it gets one attempt even
    # though the normal queue excludes deferred rows.  It is re-deferred on
    # any further per-file failure rather than becoming an unattended loop.
    # Only removed in memory: if the retry is interrupted, the saved file
    # still holds the decision.
    deferred["decisions"].pop(key, None)
    result = encode_one(
        record, config, ffmpeg, ffprobe,
        prompt_on_source_errors=action == "retry",
        allow_source_errors=action == "encode-anyway",
    )
    save_result(config, state, record, result)
    if not result.ok and result.status in DEFERRED_DECISION_STATUSES:
        defer_record(record, result, output_path(record, config), deferred)
        save_deferred(config, deferred)
        print(f"Still deferred: {record['relative_path']} ({result.status.replace('_', ' ')}).", file=sys.stderr)
    elif not result.ok and result.status != "insufficient_space":
        # Interrupted or otherwise unfinished: the decision was not made, so
        # put the item back rather than releasing it to unattended runs.
        deferred["decisions"][key] = item
        save_deferred(config, deferred)
        print(f"Left deferred: {record['relative_path']} ({result.status.replace('_', ' ')}).", file=sys.stderr)
    else:
        save_deferred(config, deferred)
        if result.ok:
            print(f"Deferred decision completed: {record['relative_path']} ({result.status.replace('_', ' ')}).")
    return 0


def main() -> int:
    global STOP_AFTER_CURRENT, STOP_INPUT_ENABLED, QUEUE_LOCK_FD
    STOP_AFTER_CURRENT = False
    STOP_INPUT_ENABLED = False
    parser = argparse.ArgumentParser(
        description="Rescan and run a safe, resumable hardware-HEVC queue.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("config", help="queue JSON configuration")
    parser.add_argument("--files", type=int, help="encode at most this many new files")
    parser.add_argument(
        "--hours",
        type=float,
        help="soft wall-clock limit; 0 runs until stopped or complete; never interrupts a file",
    )
    parser.add_argument("--dry-run", action="store_true", help="rescan and show the next jobs without encoding")
    parser.add_argument("--status", action="store_true", help="rescan and show queue status without encoding")
    parser.add_argument(
        "--rank-folders",
        action="store_true",
        help="show pending movie/series folders ranked by estimated savings",
    )
    parser.add_argument("--folder", help="limit work to a library-relative movie/series folder")
    parser.add_argument('--resolve-folder', type=int, help='print stable folder name from cached ranking')
    parser.add_argument(
        "--folder-index",
        type=int,
        help="select the ranked folder number (as shown by --rank-folders)",
    )
    parser.add_argument("--limit", type=int, default=10, help="number of ranked folders to display")
    parser.add_argument("--rescan-only", action="store_true", help="update CSV/JSON inventory and stop")
    parser.add_argument("--no-rescan", action="store_true", help="use the existing JSON inventory")
    parser.add_argument("--all", action="store_true", help="explicitly run every pending job")
    parser.add_argument("--deferred-decisions", action="store_true", help="list files held for a manual decision")
    parser.add_argument(
        "--release-deferred", action="store_true",
        help="let every deferred item without a held output into the next run for one more attempt",
    )
    parser.add_argument(
        "--recheck-deferred", action="store_true",
        help="re-verify every held verification-failed output under current rules; promote those that pass",
    )
    parser.add_argument("--deferred-index", type=int, help="number from --deferred-decisions to resolve")
    parser.add_argument(
        "--deferred-action", choices=("recheck", "retry", "encode-anyway", "trash"),
        help="re-check a held output, retry normally, override (accept a held output or encode despite "
             "source decode errors), or move the selected source to Trash",
    )
    parser.add_argument("--confirm-trash", action="store_true", help="required with --deferred-action trash")
    args = parser.parse_args()

    if args.files is not None and args.files < 0:
        parser.error("--files cannot be negative")
    if args.hours is not None and (not math.isfinite(args.hours) or args.hours < 0):
        parser.error("--hours must be zero or greater (0 means no time limit)")
    if args.all and (args.files is not None or args.hours is not None):
        parser.error("--all cannot be combined with --files or --hours")
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.folder_index is not None and args.folder_index <= 0:
        parser.error("--folder-index must be greater than zero")
    if args.folder_index is not None and args.folder:
        parser.error("use --folder or --folder-index, not both")
    if args.deferred_index is not None and args.deferred_index <= 0:
        parser.error("--deferred-index must be greater than zero")
    if (args.deferred_index is None) != (args.deferred_action is None):
        parser.error("use --deferred-index and --deferred-action together")
    if args.confirm_trash and args.deferred_action != "trash":
        parser.error("--confirm-trash is only valid with --deferred-action trash")
    read_only = (args.dry_run or args.status or args.rescan_only or args.rank_folders
                 or args.resolve_folder is not None or args.deferred_decisions)
    manages_deferred = args.release_deferred or args.recheck_deferred or args.deferred_action is not None
    if not (read_only or manages_deferred) and not args.all and args.files is None and args.hours is None:
        parser.error("choose --files NUMBER, --hours HOURS, --rank-folders, or explicit --all")

    config_path = Path(args.config).expanduser().resolve()
    try:
        raw_config = settings.load_ini(config_path) if config_path.suffix.lower() == ".ini" else load_json(config_path, {})
        config = validate_config(raw_config, config_path)
        ffmpeg = platform_support.find_tool("ffmpeg", config.get("ffmpeg"))
        ffprobe = platform_support.find_tool("ffprobe", config.get("ffprobe"))
        if not ffmpeg:
            raise RuntimeError("ffmpeg was not found; install it or set [tools] ffmpeg in the config")
        if not ffprobe:
            raise RuntimeError("ffprobe was not found; install it or set [tools] ffprobe in the config")
        encoder_profile = encoders.profile_for(config.get("video", {}).get("encoder", encoders.DEFAULT_ENCODER))
        HARDWARE_DECODE_ARGS[:] = encoder_profile["decode_args"]
        lock_handle = lock_queue(config)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with lock_handle:
        # Child processes retain this lock if the parent is forcibly killed,
        # preventing a restarted queue from deleting an output still being written.
        QUEUE_LOCK_FD = lock_handle.fileno()
        run_started = utc_now()
        results = Path(config["results_jsonl"])
        results_offset = results.stat().st_size if results.exists() else 0
        try:
            if not read_only and not args.release_deferred:
                cleanup_partial(config)
            records = load_inventory(config) if args.no_rescan else rescan(config, ffprobe)
            if args.rescan_only:
                return 0
            state = load_state(config)
            deferred = load_deferred(config)
            deferred_changed = prune_deferred(records, deferred)
            if adopt_previous_failures(records, state, config, deferred):
                deferred_changed = True
            if deferred_changed:
                save_deferred(config, deferred)
            if args.deferred_decisions:
                print_deferred_decisions(config, deferred)
                return 0
            if args.release_deferred:
                released, held = release_deferred_for_retry(deferred)
                save_deferred(config, deferred)
                print(f"Released {released} deferred item(s) for one more attempt in the next run.")
                if held:
                    print(f"{held} item(s) with a held output were left for re-checking instead.")
                return 0
            if args.deferred_action is not None:
                return resolve_deferred_record(args, records, config, state, deferred, ffmpeg, ffprobe)
            if args.recheck_deferred:
                promoted, left = recheck_deferred_outputs(records, config, state, deferred, ffprobe, ffmpeg)
                if not promoted and not left:
                    print("No held outputs are waiting for a re-check.")
                return 0
            if not read_only:
                # Clear rejections that the current rules no longer consider
                # problems (for example a language label the muxer added)
                # before choosing work, so those files are promoted rather
                # than waiting in the inbox.
                promoted, _left = recheck_deferred_outputs(records, config, state, deferred, ffprobe, ffmpeg)
                if promoted:
                    # Promoted files replaced their sources; the loaded
                    # inventory still describes the old H.264 files.
                    records = rescan(config, ffprobe)
            ranked = folder_ranking(records, config, state, deferred)
            if args.resolve_folder is not None:
                if not 1 <= args.resolve_folder <= len(ranked):
                    raise RuntimeError('ranked folder number is out of range')
                print(ranked[args.resolve_folder - 1]['folder'])
                return 0
            if args.rank_folders:
                print(f"\nInventory: {len(records)} files; {'cached inventory' if args.no_rescan else 'refreshed this run'}.")
                print_folder_ranking(ranked, args.limit)
                return 0

            selected_folder = args.folder
            if args.folder_index is not None:
                if args.folder_index > len(ranked):
                    print(f"error: ranked folder {args.folder_index} does not exist (only {len(ranked)} available)", file=sys.stderr)
                    return 2
                selected_folder = ranked[args.folder_index - 1]["folder"]
                print(f"\nSelected folder: {selected_folder}")
            if selected_folder:
                selected_folder = Path(selected_folder).as_posix().strip("/")
                records = [record for record in records if library_folder(record) == selected_folder]
            queue, counts = queue_rows(
                records, config, state,
                order='file_savings' if args.hours is not None else None,
                deferred=deferred,
            )
            print_status(config, counts, state)
            if args.hours is not None:
                print("  queue order: estimated space saved per file, largest saving first")

            if args.status:
                return 0
            if args.dry_run:
                visible = queue[: args.files] if args.files is not None else queue
                if args.hours:
                    print("  note: --hours is enforced using actual elapsed time during a real run")
                print("\nNext jobs:")
                for index, record in enumerate(visible, start=1):
                    rates = choose_rate(record, config)
                    print(
                        f"  {index:3}. {record['relative_path']} "
                        f"({human_bytes(as_int(record.get('size_bytes')))}, "
                        f"{human_time(as_float(record.get('duration_seconds')))}, "
                        f"{rates['target_bps'] / 1_000_000:.2f} Mb/s target)"
                    )
                if args.files is not None and visible:
                    print_capacity_report(queue, config, len(visible))
                return 0

            if not queue:
                print("\nNothing pending.")
                return 0
            if args.files == 0:
                print("\nFile limit is zero; nothing started.")
                return 0

            if args.files is not None or args.all:
                requested = min(args.files, len(queue)) if args.files is not None else len(queue)
                fit_count = print_capacity_report(queue, config, requested)
                if fit_count < requested:
                    print(
                        f"\nRefusing to start: only {fit_count} requested file(s) are expected to fit "
                        "while preserving the configured safety reserve. Choose a smaller batch."
                    )
                    return 0

            started = time.monotonic()
            encoded = 0
            accepted_existing = 0
            STOP_INPUT_ENABLED = sys.stdin.isatty()
            if STOP_INPUT_ENABLED:
                print('\nDuring the run: S then Return = finish current file and stop; Ctrl+C = interrupt now.', flush=True)
            for record in queue:
                poll_stop_input()
                if STOP_AFTER_CURRENT:
                    print('\nQueue stopped at your request; no further file will be started.')
                    break
                elapsed = time.monotonic() - started
                if args.files is not None and encoded >= args.files:
                    print(f"\nStopped after reaching the {args.files}-file limit.")
                    break
                if args.hours and elapsed >= args.hours * 3600:
                    print(f"\nStopped after reaching the {args.hours:g}-hour soft limit.")
                    break

                result = encode_one(
                    record,
                    config,
                    ffmpeg,
                    ffprobe,
                    prompt_on_source_errors=args.hours is None,
                    deadline=started + args.hours * 3600 if args.hours else None,
                )
                if result.ok:
                    if result.status == "verified":
                        encoded += 1
                    else:
                        accepted_existing += 1
                save_result(config, state, record, result)

                if result.ok and record.get("retry_requested"):
                    deferred["decisions"].pop(deferred_key(record), None)
                    save_deferred(config, deferred)

                if result.status == 'time_limit':
                    print("\nStopped after reaching the requested time limit during source checking.")
                    break
                if result.status == "insufficient_space":
                    details = result.verification.get("errors", []) if result.verification else []
                    print("\nStopped: not enough free space to safely start the next file.", file=sys.stderr)
                    for detail in details:
                        print(f"  - {detail}", file=sys.stderr)
                    return 1
                if not result.ok and result.status in DEFERRED_DECISION_STATUSES:
                    defer_record(record, result, output_path(record, config), deferred)
                    save_deferred(config, deferred)
                    details = result.verification.get("errors", []) if result.verification else []
                    if not details and result.verification:
                        details = result.verification.get("source_decode_errors", [])
                    print(
                        f"\nDeferred {result.status.replace('_', ' ')}; continuing to the next file. "
                        "Review it from Deferred decisions.",
                        file=sys.stderr,
                    )
                    for detail in details:
                        print(f"  - {detail}", file=sys.stderr)
                    continue
                if not result.ok:
                    details = result.verification.get("errors", []) if result.verification else []
                    print(f"\nStopped safely after {result.status}.", file=sys.stderr)
                    for detail in details:
                        print(f"  - {detail}", file=sys.stderr)
                    return 1
            else:
                print("\nQueue complete.")

            elapsed = time.monotonic() - started
            existing_note = f"; {accepted_existing} existing output(s) accepted" if accepted_existing else ""
            print(
                f"Run finished: {encoded} new file(s) encoded{existing_note} in {human_time(elapsed)}. "
                "Originals are retained; see the result log for promoted files."
            )
            return 0
        except KeyboardInterrupt:
            print("\nInterrupted. The source is untouched; the partial output will be redone next run.", file=sys.stderr)
            return 130
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return 2
        finally:
            QUEUE_LOCK_FD = None
            STOP_INPUT_ENABLED = False
            if not read_only and not args.release_deferred:
                try:
                    report = write_report(results, start_offset=results_offset,
                                          run_started=run_started, run_finished=utc_now(),
                                          library_root=config.get("source_root"))
                    print(f"\nReadable results: {report}", flush=True)
                except Exception as exc:
                    # Reporting must not affect the queue's saved results or exit status.
                    print(f"warning: could not refresh Markdown results: {exc}", file=sys.stderr)


if __name__ == "__main__":
    platform_support.install_signal_handlers()
    raise SystemExit(main())
