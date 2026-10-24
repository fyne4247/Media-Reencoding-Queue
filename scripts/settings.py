"""Read and write config.ini, the plain-text settings file for the queue.

The engine works with a flat dict (the same shape the old JSON config had), so
``load_ini`` just translates the INI sections into that dict. Relative paths are
left as written; the engine resolves them against the config file's folder.
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

DEFAULT_CODECS = ["h264", "avc1", "mpeg4", "mpeg2video", "mpeg1video", "vc1", "vp8"]
DEFAULT_EXTENSIONS = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm"]
DEFAULT_EXCLUDES = ["*.partial.*", "*.sample.*", "*.trickplay/*"]
# max height in pixels -> (minimum, maximum) target video bitrate in bits/second
DEFAULT_TIERS = {480: (600_000, 1_200_000), 720: (950_000, 1_800_000),
                 1080: (2_500_000, 6_000_000), 2160: (10_000_000, 22_000_000)}


class SettingsError(RuntimeError):
    pass


def _parser() -> configparser.ConfigParser:
    # interpolation=None keeps '%' in Windows/URL-ish paths literal.
    return configparser.ConfigParser(interpolation=None, delimiters=("=",))


def _items(text: str) -> list[str]:
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def load_ini(path: Path) -> dict[str, Any]:
    parser = _parser()
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except OSError as exc:
        raise SettingsError(f"cannot read config {path}: {exc}") from exc
    except configparser.Error as exc:
        raise SettingsError(f"cannot parse config {path}: {exc}") from exc

    def get(section: str, key: str, default: str = "") -> str:
        return parser.get(section, key, fallback=default).strip()

    try:
        library = get("library", "source_root")
        if not library:
            raise SettingsError(f"[library] source_root is not set in {path}")
        data_dir = get("data", "data_dir", "data")
        tiers = []
        tier_section = dict(parser.items("bitrate_tiers")) if parser.has_section("bitrate_tiers") else {}
        if tier_section:
            for height, value in tier_section.items():
                low, high = (int(item) for item in _items(value))
                tiers.append({"max_height": int(height), "minimum_bps": low, "maximum_bps": high})
        else:
            tiers = [{"max_height": h, "minimum_bps": lo, "maximum_bps": hi} for h, (lo, hi) in DEFAULT_TIERS.items()]
        tiers.sort(key=lambda tier: tier["max_height"])

        config: dict[str, Any] = {
            "name": get("library", "name", "Media library"),
            "source_root": library,
            "staging_root": get("library", "staging_root"),
            "originals_root": get("library", "originals_root"),
            "inventory_json": f"{data_dir}/video_inventory.json",
            "inventory_csv": f"{data_dir}/video_inventory.csv",
            "state_json": f"{data_dir}/reencode_state.json",
            "deferred_decisions_json": f"{data_dir}/reencode_deferred_decisions.json",
            "results_jsonl": f"{data_dir}/reencode_results.jsonl",
            "ffmpeg": get("tools", "ffmpeg") or None,
            "ffprobe": get("tools", "ffprobe") or None,
            "mpv": get("tools", "mpv") or None,
            "promote_verified": parser.getboolean("rules", "promote_verified", fallback=True),
            "allow_automatic_metadata_normalization": parser.getboolean(
                "rules", "allow_automatic_metadata_normalization", fallback=True),
            "include_codecs": _items(get("rules", "include_codecs")) or DEFAULT_CODECS,
            "extensions": _items(get("rules", "extensions")) or DEFAULT_EXTENSIONS,
            "exclude_globs": _items(get("rules", "exclude_globs")) or DEFAULT_EXCLUDES,
            "skip_folders": _items(get("rules", "skip_folders")) or ["Originals"],
            "order": get("rules", "order", "folder_savings"),
            "minimum_free_gib": parser.getfloat("rules", "minimum_free_gib", fallback=10),
            "minimum_savings_percent": parser.getfloat("rules", "minimum_savings_percent", fallback=5),
            "minimum_predicted_savings_percent": parser.getfloat(
                "rules", "minimum_predicted_savings_percent", fallback=10),
            "video": {
                "encoder": get("encoder", "encoder", "libx265"),
                "source_bitrate_ratio": parser.getfloat("encoder", "source_bitrate_ratio", fallback=0.7),
                "unknown_audio_bitrate_bps": parser.getint("encoder", "unknown_audio_bitrate_bps", fallback=192_000),
                "maxrate_multiplier": parser.getfloat("encoder", "maxrate_multiplier", fallback=1.5),
                "buffer_multiplier": parser.getfloat("encoder", "buffer_multiplier", fallback=2.0),
                "bitrate_tiers": tiers,
            },
        }
    except (ValueError, configparser.Error) as exc:
        raise SettingsError(f"invalid value in {path}: {exc}") from exc
    if not config["staging_root"]:
        raise SettingsError(f"[library] staging_root is not set in {path}")
    return config


TEMPLATE = """\
# Media re-encode queue settings. Lines starting with # are comments.
# Relative paths are resolved from the folder that contains this file.

[library]
# A label shown in menus and reports.
name = {name}
# The folder holding your movies and TV (searched recursively).
source_root = {source_root}
# Scratch space for encodes in progress. Must be on the same drive as the library.
staging_root = {staging_root}
# Where each original goes after its re-encode is verified. Nothing is deleted.
# Must be on the same drive as the library.
originals_root = {originals_root}

[data]
# Inventory, queue state and result logs are kept here.
data_dir = {data_dir}

[tools]
# Leave blank to use the programs found on your PATH, or give a full path.
ffmpeg = {ffmpeg}
ffprobe = {ffprobe}
# mpv is optional: it lets you preview a spot where a source has decode errors.
mpv = {mpv}

[encoder]
# libx265 (software, smallest files, slow), hevc_videotoolbox (macOS),
# hevc_nvenc (NVIDIA), hevc_qsv (Intel), hevc_amf (AMD).
encoder = {encoder}
# Target video bitrate = source video bitrate x this ratio, clamped by the tiers below.
source_bitrate_ratio = 0.7
unknown_audio_bitrate_bps = 192000
maxrate_multiplier = 1.5
buffer_multiplier = 2.0

[bitrate_tiers]
# max video height in pixels = minimum bits/s, maximum bits/s
480 = 600000, 1200000
720 = 950000, 1800000
1080 = 2500000, 6000000
2160 = 10000000, 22000000

[rules]
# Only files using these video codecs are considered. HEVC and AV1 are left alone.
include_codecs = {codecs}
extensions = {extensions}
# Folder names to skip wherever they appear (hidden dot-folders are always skipped).
skip_folders = Originals
exclude_globs = {excludes}
# folder_savings (rank whole folders), or path.
order = folder_savings
# Refuse to start an encode that would leave less than this free.
minimum_free_gib = 10
# Reject an encode that saves less than this.
minimum_savings_percent = 5
# Skip files predicted to save less than this.
minimum_predicted_savings_percent = 10
# Place the verified encode in the library and move the original to originals_root.
promote_verified = yes
# Fix harmless tag differences (blank language -> eng, untagged colour info) instead of rejecting.
allow_automatic_metadata_normalization = yes
"""


def render_ini(answers: dict[str, str]) -> str:
    values = {
        "name": "Media library", "data_dir": "data", "ffmpeg": "", "ffprobe": "", "mpv": "",
        "encoder": "libx265",
        "codecs": ", ".join(DEFAULT_CODECS), "extensions": ", ".join(DEFAULT_EXTENSIONS),
        "excludes": ", ".join(DEFAULT_EXCLUDES),
    }
    values.update(answers)
    return TEMPLATE.format(**values)
