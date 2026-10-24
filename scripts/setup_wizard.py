#!/usr/bin/env python3
"""First-run survey that writes config.ini."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import doctor
import encoders
import platform_support
import settings


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SystemExit("\nSetup cancelled.")
    return answer or default


def clean(path: str) -> str:
    """Drop the quotes that terminals add when you drag a folder in."""
    return path.strip().strip("'\"")


def run(config_path: Path) -> bool:
    print("First-time setup")
    print("================")
    print("A few questions, then your answers are saved to a plain-text file you can edit later:")
    print(f"  {config_path}\n")

    if config_path.exists():
        if ask(f"{config_path.name} already exists. Overwrite it? (y/N)", "n").lower() not in {"y", "yes"}:
            print("Keeping the existing file.")
            return False

    while True:
        raw = clean(ask("Folder containing your movies and TV (drag it in or type the path)"))
        library = Path(raw).expanduser().resolve() if raw else None
        if library and library.is_dir():
            break
        print("  That folder does not exist. Try again.")

    name = ask("A name for this library", library.name)
    # Originals and staging must share a drive with the library, so default to siblings.
    originals = clean(ask("Where should originals be kept after a verified re-encode?",
                          str(library.parent / f"{library.name} Originals")))
    staging = clean(ask("Scratch folder for encodes in progress",
                        str(library.parent / f".{library.name}-reencode-staging")))

    answers = {"name": name, "source_root": str(library),
               "originals_root": str(Path(originals).expanduser()),
               "staging_root": str(Path(staging).expanduser())}

    ffmpeg = platform_support.find_tool("ffmpeg")
    ffprobe = platform_support.find_tool("ffprobe")
    for key, found in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        if found:
            print(f"Found {key}: {found}")
        else:
            entered = clean(ask(f"{key} was not found on your PATH. Full path to it (blank to fix later)"))
            answers[key] = entered
            if key == "ffmpeg" and entered:
                ffmpeg = entered
    if platform_support.find_tool("mpv"):
        print("Found mpv (optional preview player).")

    choices = encoders.available_encoders(ffmpeg) if ffmpeg else []
    if choices:
        print("\nHEVC encoders available in your FFmpeg:")
        for index, choice in enumerate(choices, start=1):
            print(f"  {index}) {choice} - {encoders.PROFILES[choice]['label']}")
        print("  Software libx265 gives the smallest files; hardware encoders are faster but larger.")
        default = str(choices.index("libx265") + 1) if "libx265" in choices else "1"
        pick = ask("Choose an encoder number", default)
        answers["encoder"] = choices[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(choices) else choices[0]
    else:
        print("\nCould not list encoders (FFmpeg missing?). Defaulting to libx265; edit config.ini if needed.")

    config_path.write_text(settings.render_ini(answers), encoding="utf-8")
    print(f"\nSaved {config_path}\n")
    doctor.assess(config_path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.ini")
    run(parser.parse_args().config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
