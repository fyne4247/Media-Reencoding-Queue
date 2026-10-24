"""HEVC encoder profiles: how each FFmpeg encoder is invoked by the queue.

libx265 is the portable default. The hardware encoders are much faster but
make larger files at the same quality; only libx265 and hevc_videotoolbox have
been exercised by the author. The others follow FFmpeg's documented options.
"""
from __future__ import annotations

import subprocess
from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "libx265": {
        "label": "x265 (software, best compression, slow)",
        "decode_args": [],
        "extra_args": ["-preset", "medium", "-x265-params", "log-level=error"],
        "pix_fmt_10": "yuv420p10le",
        "supports_profile": True,
    },
    "hevc_videotoolbox": {
        "label": "Apple VideoToolbox (macOS hardware)",
        "decode_args": ["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld"],
        "extra_args": ["-spatial_aq", "1"],
        # Keep decoded frames on the hardware path into the encoder.
        "hw_pix_fmt": "videotoolbox_vld",
        "pix_fmt_10": "p010le",
        "supports_profile": True,
    },
    "hevc_nvenc": {
        "label": "NVIDIA NVENC (hardware)",
        "decode_args": [],
        "extra_args": ["-preset", "p5", "-spatial-aq", "1"],
        "pix_fmt_10": "p010le",
        "supports_profile": True,
    },
    "hevc_qsv": {
        "label": "Intel Quick Sync (hardware)",
        "decode_args": [],
        "extra_args": ["-preset", "slow"],
        "pix_fmt_10": "p010le",
        "supports_profile": True,
    },
    "hevc_amf": {
        "label": "AMD AMF (hardware)",
        "decode_args": [],
        "extra_args": ["-quality", "quality"],
        "pix_fmt_10": "p010le",
        "supports_profile": False,
    },
}

DEFAULT_ENCODER = "libx265"


def profile_for(name: str) -> dict[str, Any]:
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(PROFILES)
        raise RuntimeError(f"unknown encoder {name!r}; choose one of: {known}") from None


def available_encoders(ffmpeg: str) -> list[str]:
    """Names from PROFILES that this FFmpeg build lists, in preference order."""
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"], text=True, capture_output=True, check=False
    )
    listed = completed.stdout
    return [name for name in PROFILES if f" {name} " in listed]


def encoder_args(name: str, ten_bit: bool, hw_frames: bool = True) -> list[str]:
    """Codec-specific FFmpeg arguments that follow ``-c:v NAME``.

    ``hw_frames`` is False when the source was decoded in software, so frames
    arrive in system memory instead of on the hardware path.
    """
    profile = profile_for(name)
    args: list[str] = []
    if profile["supports_profile"]:
        args += ["-profile:v", "main10" if ten_bit else "main"]
    pix_fmt = profile.get("hw_pix_fmt") if hw_frames else None
    if not pix_fmt and ten_bit:
        pix_fmt = profile.get("pix_fmt_10")
    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    return args + list(profile["extra_args"])
