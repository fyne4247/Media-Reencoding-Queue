# Media Re-encoding Queue

An interactive, safety-first queue that shrinks a movie/TV library by
re-encoding H.264 (and other older codecs) to HEVC with FFmpeg. It works one
file at a time, verifies every result, and **never deletes an original**: after
a verified encode the source moves to a folder you choose.

Works on macOS, Linux and Windows. Python standard library only, so there is
nothing to `pip install` and no virtual environment is needed.

> **This tool re-encodes your files.** Re-encoding is lossy even when the result
> looks identical. Try it on a copy of a few files first, and keep the originals
> folder until you have watched the results.

## Requirements

- Python 3.9 or newer
- [FFmpeg](https://ffmpeg.org/) with `ffmpeg` and `ffprobe` on your PATH
- Optional: [mpv](https://mpv.io/), used to preview a spot where a source has decode errors

| System | Install FFmpeg |
| --- | --- |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` |
| Linux | `sudo apt install ffmpeg` (or your package manager) |

## Quick start

Run the launcher for your system from this folder:

| System | Launcher |
| --- | --- |
| macOS | `./reencode-queue.command` (or double-click it) |
| Linux | `./reencode-queue.sh` |
| Windows | `reencode-queue.bat` |

On every start it runs a **system check** (Python, FFmpeg, available HEVC
encoders, your folders, free space). The first time, a short **survey** asks for
your library folder, where originals and scratch files should go, and which
encoder to use, then saves the answers to `config.ini`. That file is plain text
with comments; edit it any time. `config.example.ini` shows every option.

`python3 scripts/menu.py --check` runs only the system check.

## Encoders

| Encoder | Notes |
| --- | --- |
| `libx265` | Software. Smallest files at a given quality, but slow. The default. |
| `hevc_videotoolbox` | macOS hardware. Fast; larger files than x265. |
| `hevc_nvenc`, `hevc_qsv`, `hevc_amf` | NVIDIA / Intel / AMD hardware. Included from FFmpeg's documented options but not yet tested by the author. |

The survey only offers encoders your FFmpeg build actually has. AV1 is not used.

## How it protects your files

- Encodes are written to a scratch folder, then checked: duration, resolution,
  colour metadata, audio and subtitle streams, chapters, size, and decode samples.
- Only a verified encode is placed in the library, and the original is moved to
  your originals folder, not deleted.
- Files that fail are held in a "deferred decisions" inbox for you to review.
- It refuses to start a file that would leave less than the configured free space.
- One queue runs at a time (file lock). Press `S` + Return during a run to stop
  after the current file; Ctrl+C interrupts immediately.
- HEVC, AV1 and files unlikely to save at least 10% are skipped.

The scratch, originals and library folders must be on the same drive so that
promotion is a fast, atomic move.

## Library layout

Ranking groups files the Jellyfin/Plex way: `Movies/<Title (Year)>/...` and
`TV/<Series>/...`. Any other layout still works; each top-level folder is ranked as one unit.

## More

- `docs/REENCODE_QUEUE_README.md`: the full behaviour of each menu option
- `scripts/build_video_inventory.py`: standalone inventory (CSV/JSON) of a folder

Run the tests:

```sh
python3 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

## License

[WTFPL](LICENSE): do what the fuck you want to. No warranty, and it does move and re-encode your files, so test on copies first.
