# How the queue works

Start it with the launcher for your system (see the main README). Every menu
option refreshes the inventory first, probing only new or changed files.

## Menu

1. **Top 10 folders.** Ranks whole `Movies/<title>` and `TV/<series>` folders
   by estimated space saved. The estimate is for prioritising only; the
   verified result after encoding is the real number.
2. **Encode a ranked folder.** Shows a dry run and capacity check, then asks
   before starting.
3. **Timed run.** Processes files across the whole library, largest estimated
   saving first, for N hours (0 = until done). It finishes the active file, then
   stops. Problem files are deferred and the run continues unattended.
4. **Status.** Totals, savings so far, and an estimate of time left.
5. **Deferred decisions.** See below.
6. **Refresh inventory only.**

During a run, type `S` + Return to stop after the current file; Ctrl+C
interrupts immediately (the partial file is discarded and redone later).

## What happens to each file

1. Skipped if its codec is not in `include_codecs`, if it is HEVC/AV1, if the
   predicted saving is under `minimum_predicted_savings_percent`, or if it is
   already recorded as done and neither file has changed.
2. The whole source video is decoded once as an integrity check (cached by size
   and modification time). On errors, an interactive run offers to open mpv just
   before the problem and asks whether to continue; a timed run defers the file.
3. One file is encoded at a time. Audio, subtitles, chapters and metadata are
   copied.
4. The output is verified: duration, resolution, colour metadata, audio and
   subtitle streams, chapters, size, and three decode samples.
5. Only then is it promoted: the encode takes the source's place as `.mkv`, and
   the source moves to `originals_root` under the same relative path. A sidecar
   `.nfo` stays beside the new file. Nothing is deleted.

If free space would drop below `minimum_free_gib`, the run stops before the next
file rather than skipping to a smaller one.

## Deferred decisions

A per-file problem (source decode errors, encode failure, failed verification,
collision, changed source) never stops an unattended run; the file is left
untouched and listed in this inbox. `[held]` marks a verification failure whose
finished encode is still in staging.

- **a** re-checks every held output with the current rules and promotes those
  that pass. Encode runs also do this automatically at the start.
- **r** gives every other deferred item one more normal attempt in the next run.
- **An item number** offers: **c** re-check, **o** override (promote a held
  output despite its mismatch, or encode despite source decode warnings, both
  logged), **r** re-encode from scratch, **t** move the source to the system
  trash after typing `TRASH`.

Language tags compare case-insensitively. With
`allow_automatic_metadata_normalization`, a track that had no language may come
out labelled by the muxer; that is logged as a warning. A track that had a real
language must keep it.

## Files

- `config.ini`: your settings
- `data/`: inventory (`.json`/`.csv`), resume state, deferred inbox
  (`reencode_deferred_decisions.json`), append-only `reencode_results.jsonl`,
  and a readable report per run in `data/runs/`
- `staging_root` and `originals_root`: the folders set in `config.ini`

Direct use of the engine: `python3 scripts/media_reencode_queue.py config.ini --help`
