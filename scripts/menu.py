#!/usr/bin/env python3
"""Interactive menu for the re-encode queue (the launchers start this)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import doctor
import platform_support
import setup_wizard
import settings

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "media_reencode_queue.py"


def ask(prompt: str) -> str | None:
    """Return the typed line, or None when input is closed (Ctrl+D / Ctrl+Z)."""
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def pause() -> None:
    ask("Press Return to continue...")


def clear() -> None:
    if sys.stdout.isatty():
        os.system("cls" if platform_support.IS_WINDOWS else "clear")


def yes(answer: str | None, default: bool = False) -> bool:
    if not answer:
        return default
    return answer.lower() in {"y", "yes"}


class Queue:
    def __init__(self, config: Path):
        self.config = config

    def run(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        """Run the queue engine. Ctrl+C is left to the engine so it can stop cleanly."""
        command = [sys.executable, str(QUEUE), str(self.config), *args]
        process = subprocess.Popen(command, stdout=subprocess.PIPE if capture else None, text=capture or None)
        while True:
            try:
                out, _ = process.communicate()
                break
            except KeyboardInterrupt:
                continue
        return subprocess.CompletedProcess(command, process.returncode, out)

    def ok(self, *args: str) -> bool:
        return self.run(*args).returncode == 0


def review_deferred(queue: Queue) -> None:
    rescan: list[str] = []
    while True:
        clear()
        # The first listing refreshes the inventory; later ones reuse it.
        if not queue.ok(*rescan, "--deferred-decisions"):
            return
        rescan = ["--no-rescan"]
        print("\n  a) Re-check every [held] output with the current rules and promote the ones that pass")
        print("  #) Enter an item number to choose what to do with that one file")
        print("  r) Retry every item without a held output in the next run (after fixing a cause)")
        print("  q) Back to the main menu\n")
        choice = ask("Choose: ")
        if not choice or choice.lower() == "q":
            return
        if choice.lower() == "a":
            print("\nEach held output is probed and spot-decoded again. Those that pass are promoted")
            print("exactly like a normal verified encode (the source moves to the originals folder).")
            print("Those that still fail stay deferred. Nothing is re-encoded.")
            if yes(ask("Re-check all held outputs now? [Y/n] "), default=True):
                queue.run("--recheck-deferred")
            else:
                print("Cancelled.")
        elif choice.lower() == "r":
            print("\nEvery deferred item without a held output gets one more attempt in the next")
            print("encode run, with normal source checks and verification. Nothing starts from here.")
            if yes(ask("Release them for the next run? [y/N] ")):
                queue.run("--no-rescan", "--release-deferred")
            else:
                print("Left deferred.")
        elif choice.isdigit():
            if int(choice) == 0:
                print("Item numbers start at 1.")
            else:
                deferred_item(queue, choice)
        else:
            print("Please enter a, r, an item number, or q.")
        print()
        if ask("Press Return to continue...") is None:
            return


def deferred_item(queue: Queue, number: str) -> None:
    print("\n  c) Re-check the held output with the current rules; promote it if it passes")
    print("  o) Override: promote the held output despite its mismatch,")
    print("     or encode despite source-decode errors")
    print("  r) Re-encode from scratch with normal source checks")
    print(f"  t) Move the source to {platform_support.trash_name()}")
    print("  q) Leave it deferred")
    decision = (ask(f"Choose an action for item {number}: ") or "").lower()
    index = ["--deferred-index", number]
    if decision == "c":
        queue.run(*index, "--deferred-action", "recheck")
    elif decision == "o":
        queue.run(*index, "--deferred-action", "encode-anyway")
    elif decision == "r":
        queue.run(*index, "--deferred-action", "retry")
    elif decision == "t":
        print(f"This moves only the selected library source to {platform_support.trash_name()}."
              " It does not permanently delete it.")
        if ask("Type TRASH to confirm: ") == "TRASH":
            queue.run(*index, "--deferred-action", "trash", "--confirm-trash")
        else:
            print("Left deferred.")
    else:
        print("Left deferred.")


def encode_folder(queue: Queue) -> None:
    if not queue.ok("--rank-folders", "--limit", "10"):
        return
    number = ask("\nEnter a ranked folder number (or q to cancel): ")
    if not number or number.lower() == "q":
        return
    if not number.isdigit() or int(number) <= 0:
        print("Please enter a positive number.")
        return
    resolved = queue.run("--no-rescan", "--resolve-folder", number, capture=True)
    folder = (resolved.stdout or "").strip()
    if resolved.returncode or not folder:
        return
    limit = ask("How many files from that folder? [1]: ") or "1"
    if not limit.isdigit():
        print("Please enter a non-negative number.")
        return
    print()
    if not queue.ok("--folder", folder, "--dry-run", "--files", limit):
        return
    print("\nThis will create and verify an HEVC copy, then place it in the library.")
    print("The source will be retained in your originals folder.")
    if yes(ask("Start this encode batch? [y/N] ")):
        queue.run("--folder", folder, "--files", limit)
    else:
        print("Cancelled.")


def timed_run(queue: Queue) -> None:
    hours = ask("How many hours should it run? [0 = until stopped or complete]: ") or ""
    try:
        value = float(hours)
    except ValueError:
        value = -1
    if value < 0:
        print("Please enter zero or a positive number of hours.")
        return
    print()
    if value == 0:
        print("This will process pending files until stopped or the queue is complete.")
    else:
        print(f"This will process pending files for about {hours} hour(s), finishing any active file.")
    print("S then Return finishes the current file and stops; Ctrl+C interrupts immediately.")
    print("Files run in descending estimated GB saved, across all movies and series.")
    print("It stops between files and preserves the configured disk-space reserve.")
    print("Verified outputs are promoted and sources retained in your originals folder.")
    if yes(ask("Start the timed run? [y/N] ")):
        queue.run("--hours", hours)
    else:
        print("Cancelled.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE.parent / "config.ini")
    parser.add_argument("--check", action="store_true", help="run the system check and exit")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()

    ready = doctor.assess(config_path, quiet=config_path.exists())
    if args.check:
        return 0 if ready else 1
    if not config_path.exists():
        if not setup_wizard.run(config_path):
            return 1
    elif not ready:
        print("Fix the items marked [FAIL] above, then run this again.")
        return 1
    try:
        config = settings.load_ini(config_path)
    except settings.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    encoder = config["video"]["encoder"]

    queue = Queue(config_path)
    print("Refreshing the library inventory...")
    if not queue.ok("--rescan-only"):
        return 1

    while True:
        clear()
        print(f"Re-encode menu: {config['name']}")
        print("=" * 40)
        print("Inventory is refreshed whenever an option runs.")
        print("Harmless metadata differences (language tag, untagged colour info) are fixed and logged, not rejected.\n")
        print("  1) Show top 10 movie/series folders by estimated savings")
        print("  2) Encode a selected top-10 folder")
        print("  3) Run by estimated savings per file (hours, or 0 for no time limit)")
        print("  4) Show overall queue status")
        print("  5) Review deferred decisions from unattended runs")
        print("  6) Refresh inventory only")
        print("  q) Quit\n")
        print(f"Encoder: {encoder}\n")
        choice = ask("Choose an option: ")
        if choice is None or choice.lower() == "q":
            return 0
        print()
        if choice == "1":
            queue.ok("--rank-folders", "--limit", "10")
        elif choice == "2":
            encode_folder(queue)
        elif choice == "3":
            timed_run(queue)
        elif choice == "4":
            queue.run("--status")
        elif choice == "5":
            review_deferred(queue)
            continue
        elif choice == "6":
            queue.run("--rescan-only")
        else:
            print("Please choose 1, 2, 3, 4, 5, 6, or q.")
        pause()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
