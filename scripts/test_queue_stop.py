"""Graceful-stop checks; no media is read or encoded."""
import contextlib
import itertools
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_reencode_queue as q


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class GracefulStopTests(unittest.TestCase):
    def test_interruption_removes_only_registered_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            stage.mkdir()
            partial = stage / 'film.partial.mkv'
            neighbor = stage / 'verified.mkv'
            neighbor.write_bytes(b'keep')
            config = {'state_json': str(root / 'state.json'), 'staging_root': str(stage)}

            def interrupted(*args, **kwargs):
                partial.write_bytes(b'incomplete')
                q.atomic_json(q.partial_marker(config), {'partial': str(partial)})
                raise KeyboardInterrupt()

            with patch.object(q, '_encode_one', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    q.encode_one({}, config, 'ffmpeg', 'ffprobe')
            self.assertFalse(partial.exists())
            self.assertFalse(q.partial_marker(config).exists())
            self.assertEqual(neighbor.read_bytes(), b'keep')
            # A restart uses the same cleanup for a durable crash marker.
            partial.write_bytes(b'crash leftover')
            q.atomic_json(q.partial_marker(config), {'partial': str(partial)})
            q.cleanup_partial(config)
            self.assertFalse(partial.exists())
            # Even a malformed marker cannot point cleanup outside staging.
            outside = root / 'original.partial.mkv'
            outside.write_bytes(b'original')
            q.atomic_json(q.partial_marker(config), {'partial': str(outside)})
            with self.assertRaises(RuntimeError):
                q.cleanup_partial(config)
            self.assertTrue(outside.exists())

    def test_completed_verification_failure_is_retained_without_overwriting_prior_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            staged = stage / 'film.mkv'
            partial = stage / 'film.partial.mkv'
            partial.write_bytes(b'first completed output')
            first = q.retain_verification_failed_output(partial, staged)
            self.assertEqual(first.name, 'film.verification-failed.mkv')
            self.assertEqual(first.read_bytes(), b'first completed output')
            self.assertFalse(partial.exists())

            partial.write_bytes(b'second completed output')
            second = q.retain_verification_failed_output(partial, staged)
            self.assertEqual(second.name, 'film.verification-failed.2.mkv')
            self.assertEqual(first.read_bytes(), b'first completed output')
            self.assertEqual(second.read_bytes(), b'second completed output')

    def tearDown(self):
        q.STOP_INPUT_ENABLED = False
        q.STOP_AFTER_CURRENT = False

    def test_stop_command_does_not_interrupt_subprocess(self):
        terminal = TerminalInput('S\n')
        q.STOP_INPUT_ENABLED = True
        with patch.object(sys, 'stdin', terminal), patch.object(q.platform_support, 'poll_stop_line', side_effect=itertools.chain(['S\n'], itertools.repeat(None))):
            result = q.run([sys.executable, '-c', 'import time; time.sleep(0.1); print("finished")'], capture=True)
        self.assertTrue(q.STOP_AFTER_CURRENT)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), 'finished')

    def test_stop_saves_current_result_and_report_without_starting_next_file(self):
        self.check_stop_run('1')

    def test_zero_hours_runs_without_deadline_and_still_stops_gracefully(self):
        self.check_stop_run('0')

    def test_encode_failure_is_deferred_and_next_file_runs(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            config = {
                'name': 'test queue', 'source_root': str(root), 'staging_root': str(root / 'staging'),
                'results_jsonl': str(root / 'results.jsonl'), 'state_json': str(root / 'state.json'),
                'deferred_decisions_json': str(root / 'deferred.json'),
                'ffmpeg': sys.executable, 'ffprobe': sys.executable,
            }
            records = [
                {'path': name, 'relative_path': name, 'size_bytes': 100, 'mtime_ns': 1, 'duration_seconds': 60}
                for name in ('one.mkv', 'two.mkv')
            ]
            state = {'jobs': {}, 'history': {}}
            outcomes = [
                q.EncodeResult(False, 'encode_failed', {'ok': False, 'errors': ['encoder failed']}, 1),
                q.EncodeResult(True, 'verified', {'ok': True, 'output_size_bytes': 50, 'saved_bytes': 50}, 1),
            ]
            stack.enter_context(patch.object(sys, 'argv', ['queue', str(root / 'config.json'), '--hours', '1']))
            for name, kwargs in {
                'validate_config': {'return_value': config},
                'lock_queue': {'side_effect': lambda _: (root / 'lock').open('w')},
                'rescan': {'return_value': records},
                'load_state': {'return_value': state},
                'folder_ranking': {'return_value': []},
                'queue_rows': {'return_value': (records, {})},
                'print_status': {}, 'poll_stop_input': {},
                'output_path': {'return_value': root / 'output.mkv'},
            }.items():
                stack.enter_context(patch.object(q, name, **kwargs))
            encoder = stack.enter_context(patch.object(q, 'encode_one', side_effect=outcomes))
            self.assertEqual(q.main(), 0)
            self.assertEqual(encoder.call_count, 2)
            deferred = json.loads((root / 'deferred.json').read_text())
            self.assertEqual(list(deferred['decisions']), ['one.mkv'])
            self.assertEqual(deferred['decisions']['one.mkv']['status'], 'encode_failed')

    def test_insufficient_space_stops_before_starting_the_next_file(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            config = {
                'name': 'test queue', 'source_root': str(root), 'staging_root': str(root / 'staging'),
                'results_jsonl': str(root / 'results.jsonl'), 'state_json': str(root / 'state.json'),
                'deferred_decisions_json': str(root / 'deferred.json'),
                'ffmpeg': sys.executable, 'ffprobe': sys.executable,
            }
            records = [
                {'path': name, 'relative_path': name, 'size_bytes': 100, 'mtime_ns': 1, 'duration_seconds': 60}
                for name in ('one.mkv', 'two.mkv')
            ]
            state = {'jobs': {}, 'history': {}}
            result = q.EncodeResult(False, 'insufficient_space', {
                'ok': False, 'errors': ['need 20 GiB; only 10 GiB is available'],
            }, 0)
            stack.enter_context(patch.object(sys, 'argv', ['queue', str(root / 'config.json'), '--hours', '1']))
            for name, kwargs in {
                'validate_config': {'return_value': config},
                'lock_queue': {'side_effect': lambda _: (root / 'lock').open('w')},
                'rescan': {'return_value': records}, 'load_state': {'return_value': state},
                'folder_ranking': {'return_value': []}, 'queue_rows': {'return_value': (records, {})},
                'print_status': {}, 'poll_stop_input': {}, 'output_path': {'return_value': root / 'output.mkv'},
            }.items():
                stack.enter_context(patch.object(q, name, **kwargs))
            encoder = stack.enter_context(patch.object(q, 'encode_one', return_value=result))
            self.assertEqual(q.main(), 1)
            self.assertEqual(encoder.call_count, 1)
            self.assertFalse((root / 'deferred.json').exists())

    def check_stop_run(self, hours):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            config = {'results_jsonl': str(root / 'results.jsonl'),
                      'state_json': str(root / 'state.json'), 'staging_root': str(root / 'staging'),
                      'ffmpeg': sys.executable, 'ffprobe': sys.executable}
            records = [{'path': name, 'size_bytes': 100, 'mtime_ns': 1} for name in ('one.mkv', 'two.mkv')]
            state = {'jobs': {}, 'history': {}}

            def encode(*args, **kwargs):
                q.STOP_AFTER_CURRENT = True
                return q.EncodeResult(True, 'verified', {'ok': True}, 1)

            stack.enter_context(patch.object(sys, 'argv', ['queue', str(root / 'config.json'), '--hours', hours]))
            mocks = {
                'validate_config': {'return_value': config},
                'lock_queue': {'side_effect': lambda _: (root / 'lock').open('w')},
                'rescan': {'return_value': records},
                'load_state': {'return_value': state},
                'folder_ranking': {'return_value': []},
                'queue_rows': {'return_value': (records, {})},
                'print_status': {},
                'poll_stop_input': {},
                'output_path': {'return_value': root / 'output.mkv'},
                'save_state': {},
            }
            for name, kwargs in mocks.items():
                mock = stack.enter_context(patch.object(q, name, **kwargs))
                if name == 'queue_rows':
                    queue_mock = mock
            encoder = stack.enter_context(patch.object(q, 'encode_one', side_effect=encode))
            self.assertEqual(q.main(), 0)
            self.assertEqual(queue_mock.call_args.kwargs['order'], 'file_savings')
            self.assertEqual(encoder.call_count, 1)
            self.assertFalse(encoder.call_args.kwargs['prompt_on_source_errors'])
            if hours == '0':
                self.assertIsNone(encoder.call_args.kwargs['deadline'])
            else:
                self.assertIsNotNone(encoder.call_args.kwargs['deadline'])
            self.assertEqual(list(state['jobs']), ['one.mkv'])
            self.assertEqual(json.loads((root / 'results.jsonl').read_text())['status'], 'verified')
            reports = list((root / 'runs').glob('*.md'))
            self.assertEqual(len(reports), 1)
            self.assertIn('one.mkv', reports[0].read_text())
            self.assertNotIn('two.mkv', reports[0].read_text())


if __name__ == '__main__':
    unittest.main()
