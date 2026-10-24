#!/usr/bin/env python3
"""Build a compact Markdown companion to a queue's JSONL results."""

import argparse
import json
from pathlib import Path


def size(value):
    return f"{value / 1024 ** 3:.2f} GiB"


def cell(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\n', ' ').replace('\r', ' ')


def display_path(source, root=None):
    """Show a source path relative to the library root when it lives under it."""
    if root:
        try:
            return Path(source).relative_to(root).as_posix()
        except ValueError:
            pass
    return str(source)


def write_report(path: Path, *, start_offset=0, run_started=None, run_finished=None, library_root=None) -> Path:
    latest = {}
    skipped = 0
    if path.exists():
        with path.open('rb') as handle:
            handle.seek(start_offset)
            content = handle.read().decode('utf-8')
    else:
        content = ''
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if entry.get('status') == 'promotion_planned':
            continue
        latest[entry['source']] = entry
    entries = sorted(latest.values(), key=lambda e: e.get('timestamp', ''), reverse=True)
    successful = [e for e in entries if e.get('status') in ('verified', 'existing_verified', 'verified_on_recheck', 'verified_with_manual_override')
                  and e.get('verification', {}).get('ok')]
    before = sum(e['verification'].get('source_size_bytes', 0) for e in successful)
    after = sum(e['verification'].get('output_size_bytes', 0) for e in successful)
    saved = before - after
    percent = saved / before * 100 if before else 0
    lines = ['# Library re-encode results', '',
             f"Latest logged result: {cell(entries[0]['timestamp']) if entries else 'No results yet'} (UTC).", '',
             f"- **Verified files:** {len(successful)}",
             f"- **Other results:** {len(entries) - len(successful)}",
             f"- **Verified file sizes:** {size(before)} → {size(after)}",
             f"- **Size reduction:** {size(saved)} ({percent:.1f}%)", '',
             'Size reduction compares verified encodes with their sources. Originals are retained; '
             'this is not disk space freed.', '',
             'Latest result per file, newest first. Earlier attempts and internal move records remain in the JSONL log.', '',
             '| File | Result | Before | After | Reduction | Encode time |',
             '| --- | --- | ---: | ---: | ---: | ---: |']
    if run_started:
        lines[2:2] = [f'Run started: {cell(run_started)} (UTC).',
                      f'Run finished: {cell(run_finished)} (UTC).', '',
                      'All totals and files below are for this run only.', '']
    for e in entries:
        v = e.get('verification') or {}
        source = display_path(e['source'], library_root)
        status = e.get('status', 'unknown').replace('_', ' ').capitalize()
        good = e in successful
        original_size = size(v['source_size_bytes']) if 'source_size_bytes' in v else '—'
        output_size = size(v['output_size_bytes']) if 'output_size_bytes' in v else '—'
        reduction = f"{size(v.get('saved_bytes', 0))} ({v.get('saved_percent', 0):.1f}%)" if good else '—'
        seconds = round(e.get('wall_seconds', 0))
        elapsed = f'{seconds // 60}m {seconds % 60:02d}s' if seconds else '—'
        lines.append('| ' + ' | '.join(map(cell, [source, status, original_size, output_size, reduction, elapsed])) + ' |')
    diagnostics = []
    for e in entries:
        verification = e.get('verification') or {}
        errors = verification.get('errors') or []
        warnings = verification.get('warnings') or []
        source_decode_errors = verification.get('source_decode_errors') or []
        if not errors and not warnings and not source_decode_errors:
            continue
        source = display_path(e['source'], library_root)
        status = e.get('status', 'unknown').replace('_', ' ')
        diagnostics.extend(['', f'## Details: {cell(source)}', '', f'Result: {cell(status)}.'])
        if errors:
            if e.get('status') == 'verification_failed':
                diagnostics.append('Verification failed because:')
            elif e.get('status') == 'encode_failed':
                diagnostics.append('FFmpeg stopped because:')
            elif e.get('status') == 'trashed':
                diagnostics.append('Manual decision:')
            else:
                diagnostics.append('Deferred because:')
            diagnostics.extend(f'- {cell(error)}' for error in errors)
        if warnings:
            diagnostics.append('Non-blocking metadata notes:')
            diagnostics.extend(f'- {cell(warning)}' for warning in warnings)
        if source_decode_errors:
            seconds = verification.get('source_error_seconds')
            location = f' at about {seconds:.2f}s' if isinstance(seconds, (int, float)) else ''
            diagnostics.append(f'Source decode was deferred because FFmpeg reported errors{location}:')
            diagnostics.extend(f'- {cell(error)}' for error in source_decode_errors)
        retained = verification.get('retained_staging_output')
        if retained:
            diagnostics.append(f'Completed HEVC output retained for review: `{cell(retained)}`')
    if diagnostics:
        lines.extend(['', '# Verification details',
                      '', 'These notes explain deferred failures and any accepted metadata normalization.'])
        lines.extend(diagnostics)
    if skipped:
        lines.extend(['', f'Note: {skipped} unreadable log line(s) omitted.'])
    destination = path.with_suffix('.md')
    if run_started:
        stamp = run_started.replace(':', '-').replace('+00-00', 'Z')
        destination = path.parent / 'runs' / f'{path.stem}_{stamp}.md'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary.replace(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results_jsonl', type=Path)
    args = parser.parse_args()
    print(write_report(args.results_jsonl))
