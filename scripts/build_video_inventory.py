#!/usr/bin/env python3
"""Build CSV and JSON inventories of local video files using ffprobe."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".webm",
    ".wmv",
}

CSV_FIELDS = [
    "root",
    "path",
    "filename",
    "extension",
    "size_bytes",
    "size_gib",
    "duration_seconds",
    "duration_hours",
    "container",
    "overall_bitrate_bps",
    "video_codec",
    "video_profile",
    "video_width",
    "video_height",
    "video_fps",
    "video_bitrate_bps",
    "video_pixel_format",
    "video_stream_count",
    "attached_picture_count",
    "audio_stream_count",
    "audio_codecs",
    "audio_channels",
    "audio_bitrates_bps",
    "subtitle_stream_count",
    "subtitle_codecs",
    "candidate_hint",
    "ffprobe_error",
]


def number(value: Any) -> float | int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def fps(stream: dict[str, Any]) -> float | None:
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not rate or rate in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = rate.split("/", 1)
        return round(float(numerator) / float(denominator), 3)
    except (ValueError, ZeroDivisionError):
        return None


def streams_of_type(streams: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [stream for stream in streams if stream.get("codec_type") == kind]


def codec_names(streams: list[dict[str, Any]]) -> str:
    return ";".join(sorted({str(s.get("codec_name")) for s in streams if s.get("codec_name")}))


def candidate_hint(codec: str | None, size_bytes: int) -> str:
    codec = (codec or "").lower()
    if codec in {"h264", "avc1", "mpeg4", "mpeg2video", "mpeg1video", "vc1", "vp8"}:
        return "good first candidate"
    if codec in {"hevc", "h265", "av1", "vp9"}:
        return "already modern; inspect carefully"
    if size_bytes < 500 * 1024 * 1024:
        return "small file; low priority"
    return "inspect"


def probe(path: Path, root_label: str, ffprobe: str, scanned_at: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "root": root_label,
        "path": str(path),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": 0,
        "scanned_at": scanned_at,
    }
    try:
        result['size_bytes'] = path.stat().st_size
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format:stream",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode:
            result["ffprobe_error"] = completed.stderr.strip() or f"exit {completed.returncode}"
            return finish(result)
        data = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        result["ffprobe_error"] = str(exc)
        return finish(result)

    streams = data.get("streams", [])
    format_data = data.get("format", {})
    videos = streams_of_type(streams, "video")
    audios = streams_of_type(streams, "audio")
    subtitles = streams_of_type(streams, "subtitle")
    main_video = next((s for s in videos if not s.get("disposition", {}).get("attached_pic")), videos[0] if videos else {})
    attached = [s for s in videos if s.get("disposition", {}).get("attached_pic")]

    result.update(
        {
            "duration_seconds": number(format_data.get("duration")),
            "container": format_data.get("format_name"),
            "overall_bitrate_bps": number(format_data.get("bit_rate")),
            "video_codec": main_video.get("codec_name"),
            "video_profile": main_video.get("profile"),
            "video_width": number(main_video.get("width")),
            "video_height": number(main_video.get("height")),
            "video_fps": fps(main_video),
            "video_bitrate_bps": number(main_video.get("bit_rate")),
            "video_pixel_format": main_video.get("pix_fmt"),
            "video_stream_count": len(videos),
            "attached_picture_count": len(attached),
            "audio_stream_count": len(audios),
            "audio_codecs": codec_names(audios),
            "audio_channels": ";".join(str(s.get("channels")) for s in audios if s.get("channels")),
            "audio_bitrates_bps": ";".join(str(number(s.get("bit_rate"))) for s in audios if number(s.get("bit_rate")) is not None),
            "subtitle_stream_count": len(subtitles),
            "subtitle_codecs": codec_names(subtitles),
            "candidate_hint": candidate_hint(main_video.get("codec_name"), result["size_bytes"]),
        }
    )
    return finish(result)


def finish(result: dict[str, Any]) -> dict[str, Any]:
    size = result.get("size_bytes")
    duration = result.get("duration_seconds")
    result["size_gib"] = round(size / 1024**3, 3) if size is not None else None
    result["duration_hours"] = round(duration / 3600, 3) if duration is not None else None
    result.setdefault("candidate_hint", candidate_hint(result.get("video_codec"), size or 0))
    result.setdefault("ffprobe_error", "")
    for field in CSV_FIELDS:
        result.setdefault(field, "")
    return result


def discover(root: Path, include_ts: bool = False, skip: tuple[str, ...] = ('originals',)) -> list[tuple[str, Path]]:
    if not root.exists():
        print(f"warning: missing root: {root}", file=sys.stderr)
        return []
    label = str(root)
    paths = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not name.startswith('.')
                          and name.casefold() not in {item.casefold() for item in skip}
                          and not name.endswith('.trickplay')
                          and not (Path(current) / name).is_symlink()]
        for name in filenames:
            path = Path(current) / name
            if not path.is_symlink() and (path.suffix.lower() in VIDEO_EXTENSIONS or (include_ts and path.suffix.lower() == '.ts')):
                paths.append(path)
    return [(label, path) for path in paths]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "roots",
        nargs="*",
        default=None,
        help="folders to scan recursively (required)",
    )
    parser.add_argument("--csv", default=None)
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--skip", action="append", default=["Originals"], metavar="FOLDER",
                        help="folder name to skip wherever it appears (repeatable; default: Originals)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--include-ts",
        action="store_true",
        help="include .ts files (off by default because .ts also means TypeScript source)",
    )
    args = parser.parse_args()

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        parser.error("ffprobe was not found on PATH")

    project_dir = Path(__file__).resolve().parent.parent
    if not args.roots:
        parser.error("give at least one folder to scan")
    scan_roots = args.roots
    inventory_dir = project_dir / "data"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.csv or str(inventory_dir / "standalone_video_inventory.csv")
    json_path = args.json_path or str(inventory_dir / "standalone_video_inventory.json")
    if Path(csv_path).resolve() == Path(json_path).resolve():
        parser.error('CSV and JSON outputs must be different files')
    for destination in (csv_path, json_path):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)

    items: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for raw_root in scan_roots:
        root = Path(raw_root).expanduser().resolve()
        for label, path in discover(root, include_ts=args.include_ts, skip=tuple(args.skip)):
            resolved = path.resolve()
            if resolved not in seen:
                items.append((label, resolved))
                seen.add(resolved)

    scanned_at = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    print(f"Probing {len(items)} video files with {args.workers} workers...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(probe, path, label, ffprobe, scanned_at) for label, path in items]
        for index, future in enumerate(as_completed(futures), start=1):
            records.append(future.result())
            if index % 100 == 0 or index == len(futures):
                print(f"  {index}/{len(futures)}", file=sys.stderr)

    records.sort(key=lambda item: (item.get("root", ""), item.get("path", "").lower()))
    for record in records:
        record.pop("scanned_at", None)

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"generated_at": scanned_at, "files": records}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(records)

    print(f"Wrote {len(records)} records to {csv_path} and {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
