"""Regression checks using temporary files; never encodes library media."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_reencode_queue as q
import build_video_inventory as inventory


class MediaTests(unittest.TestCase):
    def test_file_savings_crosses_folders_without_changing_folder_ranking(self):
        # A wins in aggregate; B wins per file, despite a smaller source.
        records = [
            {'path': name, 'relative_path': name, 'size_bytes': size, 'mtime_ns': 1,
             'saving': saving}
            for name, size, saving in [
                ('TV/A/Season 01/02.mkv', 1000, 60),
                ('TV/B/Season 01/01.mkv', 200, 100),
                ('TV/A/Season 01/01.mkv', 1000, 60),
            ]
        ]
        def estimate(record, config):
            return {'estimated_saved_bytes': record['saving']}
        with patch.object(q, 'eligible', return_value=(True, 'eligible')), \
             patch.object(q, 'output_path', return_value=Path('/unused')), \
             patch.object(q, 'estimated_savings', side_effect=estimate):
            config = {'order': 'folder_savings'}
            folder_before = q.folder_ranking(records, config, {})
            rows, counts = q.queue_rows(records, config, {}, order='file_savings')
            self.assertEqual([r['relative_path'] for r in rows], [
                'TV/B/Season 01/01.mkv', 'TV/A/Season 01/01.mkv', 'TV/A/Season 01/02.mkv'])
            self.assertEqual(counts['pending'], 3)
            self.assertEqual(q.folder_ranking(records, config, {}), folder_before)
            self.assertEqual([g['folder'] for g in folder_before], ['TV/A', 'TV/B'])
            default_rows, _ = q.queue_rows(records, config, {})
            self.assertEqual([q.library_folder(r) for r in default_rows], ['TV/A', 'TV/A', 'TV/B'])

    def test_clean_decode_cache_invalidates_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.mkv'
            source.write_bytes(b'original')
            config = {'state_json': str(Path(tmp) / 'state.json')}
            def record():
                stat = source.stat()
                return {'path': str(source), 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            with patch.object(q, 'source_decode_check', return_value={'errors': [], 'seconds': 0}) as check:
                q.checked_source(record(), config, 'ffmpeg')
                q.checked_source(record(), config, 'ffmpeg')
                self.assertEqual(check.call_count, 1)
                source.write_bytes(b'changed source')
                q.checked_source(record(), config, 'ffmpeg')
                self.assertEqual(check.call_count, 2)

    def test_hardware_decode_errors_fall_back_to_software_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.mkv'
            source.write_bytes(b'original')
            config = {'state_json': str(Path(tmp) / 'state.json')}
            stat = source.stat()
            record = {'path': str(source), 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            calls = []

            def check(path, ffmpeg, decode_args=None):
                calls.append(decode_args)
                if decode_args is None:
                    return {'errors': ['hardware accelerator failed to decode picture'], 'seconds': 5.0}
                return {'errors': [], 'seconds': 0.0}

            with patch.object(q, 'HARDWARE_DECODE_ARGS', ['-hwaccel', 'videotoolbox']), \
                    patch.object(q, 'source_decode_check', side_effect=check):
                result = q.checked_source(record, config, 'ffmpeg')
                self.assertEqual(calls, [None, []])
                self.assertEqual(result['errors'], [])
                self.assertTrue(result['software_decode'])
                # The verdict is cached, including that software decode is required.
                again = q.checked_source(record, config, 'ffmpeg')
                self.assertTrue(again['software_decode'])
                self.assertEqual(len(calls), 2)

    def test_errors_in_both_decoders_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.mkv'
            source.write_bytes(b'original')
            config = {'state_json': str(Path(tmp) / 'state.json')}
            stat = source.stat()
            record = {'path': str(source), 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            with patch.object(q, 'HARDWARE_DECODE_ARGS', ['-hwaccel', 'videotoolbox']), \
                    patch.object(q, 'source_decode_check', return_value={'errors': ['corrupt'], 'seconds': 9.0}):
                result = q.checked_source(record, config, 'ffmpeg')
            self.assertEqual(result['errors'], ['corrupt'])
            self.assertFalse(result['software_decode'])

    def test_decode_checks_preserve_timestamp_precision(self):
        hardware = ['-hwaccel', 'videotoolbox', '-hwaccel_output_format', 'videotoolbox_vld']
        with patch.object(q, 'HARDWARE_DECODE_ARGS', hardware), \
                patch.object(q, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as runner:
            q.source_decode_check(Path('dummy'), 'ffmpeg')
            q.spot_decode(Path('dummy'), 60, 'ffmpeg')
            for call in runner.call_args_list:
                command = call.args[0]
                self.assertEqual(command[command.index('-hwaccel') + 1], 'videotoolbox')
                self.assertEqual(command[command.index('-hwaccel_output_format') + 1], 'videotoolbox_vld')
                self.assertEqual(command[command.index('-fps_mode:v') + 1], 'passthrough')
                self.assertEqual(command[command.index('-enc_time_base:v') + 1], 'demux')

    def test_relative_config_paths_resolve_from_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'Media Library Project' / 'config' / 'queue.json'
            source = root / 'library'
            staging = root / '.reencode-staging' / 'library'
            originals = root / 'Originals'
            for path in (config_path.parent, source, staging, originals):
                path.mkdir(parents=True, exist_ok=True)
            config = {
                'name': 'temporary queue',
                'source_root': '../../library',
                'staging_root': '../../.reencode-staging/library',
                'originals_root': '../../Originals',
                'inventory_json': '../inventories/video_inventory.json',
                'inventory_csv': '../inventories/video_inventory.csv',
                'state_json': '../state/state.json',
                'results_jsonl': '../logs/results.jsonl',
                'promote_verified': True,
            }
            resolved = q.validate_config(config, config_path)
            self.assertEqual(Path(resolved['source_root']), source.resolve())
            self.assertEqual(Path(resolved['staging_root']), staging.resolve())
            self.assertEqual(Path(resolved['originals_root']), originals.resolve())
            self.assertEqual(Path(resolved['inventory_json']), (root / 'Media Library Project' / 'inventories' / 'video_inventory.json').resolve())

    def test_old_mount_paths_are_relocated_in_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_source = root / 'library'
            current_staging = root / '.reencode-staging' / 'library'
            current_originals = root / 'Originals'
            for path in (current_source, current_staging, current_originals):
                path.mkdir(parents=True)
            state_path = root / 'state.json'
            state_path.write_text(json.dumps({
                'source_root': '/library-root/library',
                'staging_root': '/library-root/.reencode-staging/library',
                'jobs': {
                    '/library-root/library/TV/Episode.mkv': {
                        'output': '/library-root/.reencode-staging/library/TV/Episode.mkv',
                        'original': '/library-root/Originals/TV/Episode.mkv',
                    }
                },
                'history': [],
            }), encoding='utf-8')
            state = q.load_state({
                'name': 'temporary queue',
                'source_root': str(current_source),
                'staging_root': str(current_staging),
                'originals_root': str(current_originals),
                'state_json': str(state_path),
            })
            job = state['jobs'][str(current_source / 'TV/Episode.mkv')]
            self.assertEqual(job['output'], str(current_staging / 'TV/Episode.mkv'))
            self.assertEqual(job['original'], str(current_originals / 'TV/Episode.mkv'))
            self.assertEqual(state['source_root'], str(current_source))
            self.assertEqual(state['staging_root'], str(current_staging))

    def test_timestamp_zero_and_progress(self):
        for progress, expected in [('00:00:00.000', 0), ('00:25:13.000', 1513)]:
            output = f'out_time={progress}\n[matroska] invalid EBML\nout_time=02:00:00.000\nelapsed=10\n'
            with patch.object(q, 'run', return_value=subprocess.CompletedProcess([], 0, '', output)):
                result = q.source_decode_check(Path('dummy'), 'ffmpeg')
            self.assertEqual(result['seconds'], expected)
            self.assertEqual(result['errors'], ['[matroska] invalid EBML'])

    def test_movie_collection_grouping(self):
        self.assertEqual(q.library_folder({'relative_path': 'Movies/Twilight/Twilight (2008)/movie.mkv'}),
                         'Movies/Twilight/Twilight (2008)')
        self.assertEqual(q.library_folder({'relative_path': 'TV/Series/Season 01/e.mkv'}), 'TV/Series')

    def test_promotion_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'library' / 'Movies' / 'Film' / 'film.mkv'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'original')
            staged = root / 'staged.mkv'
            staged.write_bytes(b'encode')
            config = {'promote_verified': True, 'source_root': str(root / 'library'), 'originals_root': str(root / 'Originals')}
            result = q.promote_verified(source, staged, source, config)
            self.assertEqual(source.read_bytes(), b'encode')
            self.assertEqual(Path(result['original']).read_bytes(), b'original')
            staged.write_bytes(b'second')
            with self.assertRaises(RuntimeError):
                q.promote_verified(source, staged, source, config)
            self.assertEqual(source.read_bytes(), b'encode')

    def test_promotion_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'library' / 'film.mkv'
            source.parent.mkdir()
            source.write_bytes(b'original')
            config = {'promote_verified': True, 'source_root': str(source.parent), 'originals_root': str(root / 'Originals')}
            with self.assertRaises(OSError):
                q.promote_verified(source, root / 'missing', source, config)
            self.assertEqual(source.read_bytes(), b'original')

    def test_deferral_no_prompt_and_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'library' / 'film.mkv'
            source.parent.mkdir()
            source.write_bytes(b'original')
            stat = source.stat()
            record = {'path': str(source), 'relative_path': source.name, 'size_bytes': stat.st_size,
                      'mtime_ns': stat.st_mtime_ns, 'duration_seconds': 60}
            config = {'source_root': str(source.parent), 'staging_root': str(root / 'stage'),
                      'state_json': str(root / 'state.json')}
            with patch.object(q, 'free_space_ok', return_value=(True, 'space')), \
                 patch.object(q, 'source_decode_check', return_value={'errors': ['bad'], 'seconds': 5}), \
                 patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
                result = q.encode_one(record, config, 'ffmpeg', 'ffprobe', False)
                self.assertEqual(result.status, 'source_decode_deferred')
                result = q.encode_one(record, config, 'ffmpeg', 'ffprobe', False, deadline=0)
                self.assertEqual(result.status, 'time_limit')

    def test_discovery_protected_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ('Movies', 'Skipme', 'Originals'):
                (root / directory).mkdir()
                (root / directory / 'film.mkv').touch()
            (root / 'Movies' / 'link.mkv').symlink_to(root / 'Skipme' / 'film.mkv')
            found = inventory.discover(root, skip=('Skipme', 'Originals'))
            self.assertEqual([p.relative_to(root).as_posix() for _, p in found], ['Movies/film.mkv'])
            config = {'source_root': str(root), 'staging_root': str(root / 'stage'), 'skip_folders': ['Skipme', 'Originals']}
            self.assertEqual(q.discover(config), [(root / 'Movies' / 'film.mkv').resolve()])

    def test_capacity_uses_same_estimate(self):
        record = {'duration_seconds': 100, 'size_bytes': 100000000, 'video_height': 720, 'audio_stream_count': 1}
        with tempfile.TemporaryDirectory() as tmp:
            config = {'staging_root': tmp, 'minimum_free_gib': 0}
            estimate = q.capacity_bytes(record, config)
            usage = type('Usage', (), {'free': estimate + 1})()
            with patch.object(q.shutil, 'disk_usage', return_value=usage), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(q.print_capacity_report([record, record], config, 2), 1)
                self.assertTrue(q.free_space_ok(record, q.choose_rate(record, config), config)[0])

    def test_default_subtitle_assignment_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.mkv'
            output = Path(tmp) / 'output.mkv'
            source.write_bytes(b'original')
            output.write_bytes(b'new output')
            def data(default, forced=0, size='8', video_codec='h264'):
                return {
                    'format': {'duration': '60', 'size': size},
                    'streams': [
                        {'codec_type': 'video', 'codec_name': video_codec, 'width': 1920, 'height': 1080,
                         'pix_fmt': 'yuv420p'},
                        {'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'eng'},
                         'disposition': {'default': default, 'forced': forced}},
                    ],
                }
            with patch.object(q, 'probe_file', side_effect=[data(0), data(1, size='4', video_codec='hevc')]), \
                 patch.object(q, 'spot_decode', return_value=[]):
                result = q.verify(source, output, {'allow_automatic_metadata_normalization': True}, 'ffprobe', 'ffmpeg')
            self.assertTrue(result['ok'])
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['warnings'], ['subtitle disposition flags were normalized'])
            with patch.object(q, 'probe_file', side_effect=[data(0), data(1, forced=1, size='4', video_codec='hevc')]), \
                 patch.object(q, 'spot_decode', return_value=[]):
                result = q.verify(source, output, {'allow_automatic_metadata_normalization': True}, 'ffprobe', 'ffmpeg')
            self.assertTrue(result['ok'])
            self.assertEqual(result['warnings'], ['subtitle disposition flags were normalized'])

    def test_audio_difference_reports_the_specific_changed_field(self):
        source = {
            'streams': [{
                'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
                'sample_rate': '48000', 'channel_layout': 'stereo',
                'tags': {'language': 'und'}, 'disposition': {'default': 1},
            }],
        }
        output = {
            'streams': [{
                'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
                'sample_rate': '48000', 'channel_layout': 'stereo',
                'tags': {'language': 'eng'}, 'disposition': {'default': 1},
            }],
        }
        self.assertEqual(
            q.audio_differences(source, output),
            ["audio stream 1 language changed: 'und' -> 'eng'"],
        )

    def test_verification_failure_retains_completed_staging_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / 'library'
            source_root.mkdir()
            source = source_root / 'film.mp4'
            source.write_bytes(b'source')
            stat = source.stat()
            staging = root / 'staging'
            partial = staging / 'film.partial.mkv'
            record = {
                'path': str(source), 'relative_path': 'film.mp4',
                'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'duration_seconds': 60, 'video_height': 720,
                'audio_stream_count': 1, 'video_pixel_format': 'yuv420p',
            }
            config = {
                'source_root': str(source_root), 'staging_root': str(staging),
                'state_json': str(root / 'state.json'), 'promote_verified': False,
            }
            def complete_encode(*_args, **_kwargs):
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b'completed HEVC')
                return subprocess.CompletedProcess([], 0, '', '')
            with patch.object(q, 'free_space_ok', return_value=(True, 'space')), \
                 patch.object(q, 'checked_source', return_value={'errors': [], 'seconds': 0}), \
                 patch.object(q, 'run', side_effect=complete_encode), \
                 patch.object(q, 'verify', return_value={'ok': False, 'errors': ['audio metadata changed']}):
                result = q.encode_one(record, config, 'ffmpeg', 'ffprobe')
            retained = staging / 'film.verification-failed.mkv'
            self.assertEqual(result.status, 'verification_failed')
            self.assertEqual(result.verification['retained_staging_output'], str(retained))
            self.assertEqual(retained.read_bytes(), b'completed HEVC')
            self.assertFalse(partial.exists())
            self.assertFalse(q.partial_marker(config).exists())

    def test_accept_retained_verification_output_promotes_after_manual_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / 'library'
            source = source_root / 'Movies' / 'Film (2026)' / 'Film.mp4'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'original MP4')
            stat = source.stat()
            staging = root / 'staging'
            held = staging / 'Movies' / 'Film (2026)' / 'Film.verification-failed.mkv'
            held.parent.mkdir(parents=True)
            held.write_bytes(b'accepted HEVC')
            record = {
                'path': str(source), 'relative_path': 'Movies/Film (2026)/Film.mp4',
                'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            }
            config = {
                'source_root': str(source_root), 'staging_root': str(staging),
                'originals_root': str(root / 'Originals'), 'promote_verified': True,
            }
            item = {
                'retained_staging_output': str(held),
                'verification': {
                    'ok': False, 'errors': ["audio stream 1 language changed: 'und' -> ''"],
                    'source_size_bytes': stat.st_size, 'output_size_bytes': held.stat().st_size,
                },
            }
            result = q.accept_retained_verification_output(record, item, config)
            final = source.with_suffix('.mkv')
            original = root / 'Originals' / 'Movies' / 'Film (2026)' / 'Film.mp4'
            self.assertTrue(result.ok)
            self.assertEqual(result.status, 'verified_with_manual_override')
            self.assertFalse(source.exists())
            self.assertEqual(original.read_bytes(), b'original MP4')
            self.assertEqual(final.read_bytes(), b'accepted HEVC')
            self.assertIn("Manually accepted despite verification mismatch", result.verification['warnings'][0])

    @staticmethod
    def _language_probe(language_tags, *, size='8', video_codec='h264'):
        return {
            'format': {'duration': '60', 'size': size},
            'streams': [
                {'codec_type': 'video', 'codec_name': video_codec, 'width': 1920, 'height': 1080,
                 'pix_fmt': 'yuv420p'},
                {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2, 'sample_rate': '48000',
                 'tags': language_tags, 'disposition': {'default': 1}},
            ],
        }

    def _verify_languages(self, source_tags, output_tags, normalize=True):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.mkv'
            output = Path(tmp) / 'output.mkv'
            source.write_bytes(b'original')
            output.write_bytes(b'new')
            probes = [self._language_probe(source_tags),
                      self._language_probe(output_tags, size='4', video_codec='hevc')]
            with patch.object(q, 'probe_file', side_effect=probes), \
                 patch.object(q, 'spot_decode', return_value=[]):
                return q.verify(source, output, {'allow_automatic_metadata_normalization': normalize},
                                'ffprobe', 'ffmpeg')

    def test_uppercase_language_tag_matches_ffmpeg_lowercase_tag(self):
        # mkvmerge-style LANGUAGE=eng is rewritten by FFmpeg as language=eng.
        result = self._verify_languages({'LANGUAGE': 'eng'}, {'language': 'eng'}, normalize=False)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['warnings'], [])

    def test_language_added_to_untagged_track_is_a_warning(self):
        for source_tags in ({}, {'language': ''}, {'language': 'und'}):
            result = self._verify_languages(source_tags, {'language': 'eng'})
            self.assertTrue(result['ok'], (source_tags, result['errors']))
            self.assertEqual(len(result['warnings']), 1)
            self.assertIn("output is labeled 'eng'", result['warnings'][0])

    def test_real_language_change_is_still_rejected(self):
        result = self._verify_languages({'language': 'jpn'}, {'language': 'eng'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'], ["audio stream 1 language changed: 'jpn' -> 'eng'"])
        strict = self._verify_languages({}, {'language': 'eng'}, normalize=False)
        self.assertFalse(strict['ok'])

    def _deferred_fixture(self, root):
        source_root = root / 'library'
        source = source_root / 'TV' / 'Show (2000)' / 'Season 01' / 'Show - S01E01.mkv'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'original H.264')
        stat = source.stat()
        staging = root / 'staging'
        held = staging / 'TV' / 'Show (2000)' / 'Season 01' / 'Show - S01E01.verification-failed.mkv'
        held.parent.mkdir(parents=True)
        held.write_bytes(b'held HEVC')
        record = {
            'path': str(source), 'relative_path': 'TV/Show (2000)/Season 01/Show - S01E01.mkv',
            'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'duration_seconds': 60,
        }
        config = {
            'source_root': str(source_root), 'staging_root': str(staging),
            'originals_root': str(root / 'Originals'), 'promote_verified': True,
            'state_json': str(root / 'state.json'), 'results_jsonl': str(root / 'results.jsonl'),
            'deferred_decisions_json': str(root / 'deferred.json'),
        }
        deferred = q.deferred_template(config)
        result = q.EncodeResult(False, 'verification_failed', {
            'ok': False, 'errors': ["audio stream 1 language changed: '' -> 'eng'"],
            'retained_staging_output': str(held),
        }, 1.0)
        q.defer_record(record, result, source, deferred)
        return source, held, record, config, deferred

    def test_recheck_promotes_held_output_that_now_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, held, record, config, deferred = self._deferred_fixture(root)
            state = q.state_template({**config, 'name': 'test'})
            passing = {'ok': True, 'errors': [], 'warnings': [], 'saved_bytes': 5, 'output_size_bytes': 9}
            with patch.object(q, 'verify', return_value=passing):
                promoted, left = q.recheck_deferred_outputs([record], config, state, deferred, 'ffprobe', 'ffmpeg')
            self.assertEqual((promoted, left), (1, 0))
            self.assertEqual(deferred['decisions'], {})
            self.assertEqual(source.read_bytes(), b'held HEVC')
            self.assertEqual((root / 'Originals' / record['relative_path']).read_bytes(), b'original H.264')
            self.assertEqual(state['jobs'][str(source)]['status'], 'verified_on_recheck')
            # A recheck took no encode time, so it must not skew the speed history.
            self.assertEqual(state['history']['verified_files'], 0)
            self.assertEqual(json.loads((root / 'deferred.json').read_text())['decisions'], {})

    def test_recheck_keeps_held_output_deferred_when_it_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, held, record, config, deferred = self._deferred_fixture(root)
            state = q.state_template({**config, 'name': 'test'})
            failing = {'ok': False, 'errors': ['chapter count changed'], 'warnings': []}
            with patch.object(q, 'verify', return_value=failing):
                promoted, left = q.recheck_deferred_outputs([record], config, state, deferred, 'ffprobe', 'ffmpeg')
            self.assertEqual((promoted, left), (0, 1))
            item = deferred['decisions'][record['relative_path']]
            self.assertEqual(item['verification']['errors'], ['chapter count changed'])
            self.assertEqual(item['retained_staging_output'], str(held))
            self.assertEqual(source.read_bytes(), b'original H.264')
            self.assertTrue(held.exists())

    def test_release_lets_unheld_items_into_one_more_run(self):
        records = [
            {'path': f'/lib/{name}.mkv', 'relative_path': f'TV/A/{name}.mkv', 'size_bytes': 9, 'mtime_ns': 1}
            for name in ('encode', 'held')
        ]
        deferred = {'decisions': {
            'TV/A/encode.mkv': {'relative_path': 'TV/A/encode.mkv', 'status': 'encode_failed'},
            'TV/A/held.mkv': {'relative_path': 'TV/A/held.mkv', 'status': 'verification_failed',
                              'retained_staging_output': '/staging/held.verification-failed.mkv'},
        }}
        with patch.object(q, 'eligible', return_value=(True, 'eligible')):
            rows, counts = q.queue_rows(records, {}, {}, deferred=deferred)
            self.assertEqual((rows, counts['deferred']), ([], 2))
            self.assertEqual(q.release_deferred_for_retry(deferred), (1, 1))
            rows, counts = q.queue_rows(records, {}, {}, deferred=deferred)
        self.assertEqual([row['relative_path'] for row in rows], ['TV/A/encode.mkv'])
        self.assertTrue(rows[0]['retry_requested'])
        self.assertEqual(counts['deferred'], 1)
        # Another failure defers it again and withdraws the one-time release.
        failure = q.EncodeResult(False, 'encode_failed', {'ok': False, 'errors': ['x']}, 1.0)
        q.defer_record(records[0], failure, Path('/lib/encode.mkv'), deferred)
        self.assertFalse(deferred['decisions']['TV/A/encode.mkv']['retry_requested'])

    def test_completed_override_is_not_requeued(self):
        record = {'path': '/lib/a.mkv', 'relative_path': 'TV/A/a.mkv', 'size_bytes': 9, 'mtime_ns': 1}
        state = {'jobs': {'/lib/a.mkv': {'status': 'verified_with_manual_override', 'output_size_bytes': 9}}}
        _rows, counts = q.queue_rows([record], {}, state)
        self.assertEqual(counts['complete'], 1)


if __name__ == '__main__':
    unittest.main()
