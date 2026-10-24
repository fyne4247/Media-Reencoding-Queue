import tempfile
import unittest
from pathlib import Path

import encoders
import platform_support
import settings


class SettingsTests(unittest.TestCase):
    def write(self, tmp, **answers):
        path = Path(tmp) / 'config.ini'
        path.write_text(settings.render_ini({'source_root': '/lib', 'staging_root': '/stage',
                                             'originals_root': '/orig', **answers}), encoding='utf-8')
        return path

    def test_rendered_template_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings.load_ini(self.write(tmp, name='My Lib', encoder='hevc_nvenc'))
        self.assertEqual(config['name'], 'My Lib')
        self.assertEqual(config['source_root'], '/lib')
        self.assertEqual(config['video']['encoder'], 'hevc_nvenc')
        self.assertEqual([t['max_height'] for t in config['video']['bitrate_tiers']], [480, 720, 1080, 2160])
        self.assertEqual(config['video']['bitrate_tiers'][1]['maximum_bps'], 1_800_000)
        self.assertEqual(config['skip_folders'], ['Originals'])
        self.assertIsNone(config['ffmpeg'])
        self.assertTrue(config['promote_verified'])
        self.assertEqual(config['state_json'], 'data/reencode_state.json')

    def test_windows_style_paths_are_kept_literally(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings.load_ini(self.write(tmp, source_root=r'D:\Media\100% Movies'))
        self.assertEqual(config['source_root'], r'D:\Media\100% Movies')

    def test_missing_library_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.ini'
            path.write_text('[library]\nname = x\n', encoding='utf-8')
            with self.assertRaises(settings.SettingsError):
                settings.load_ini(path)

    def test_bad_number_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp)
            path.write_text(path.read_text().replace('minimum_free_gib = 10', 'minimum_free_gib = lots'))
            with self.assertRaises(settings.SettingsError):
                settings.load_ini(path)


class EncoderTests(unittest.TestCase):
    def test_software_encoder_ten_bit_arguments(self):
        args = encoders.encoder_args('libx265', ten_bit=True)
        self.assertEqual(args[args.index('-profile:v') + 1], 'main10')
        self.assertEqual(args[args.index('-pix_fmt') + 1], 'yuv420p10le')

    def test_software_encoder_eight_bit_has_no_pix_fmt(self):
        args = encoders.encoder_args('libx265', ten_bit=False)
        self.assertNotIn('-pix_fmt', args)
        self.assertEqual(args[args.index('-profile:v') + 1], 'main')

    def test_videotoolbox_keeps_frames_on_hardware(self):
        args = encoders.encoder_args('hevc_videotoolbox', ten_bit=False)
        self.assertEqual(args[args.index('-pix_fmt') + 1], 'videotoolbox_vld')
        self.assertEqual(encoders.PROFILES['hevc_videotoolbox']['decode_args'][:2], ['-hwaccel', 'videotoolbox'])

    def test_videotoolbox_with_software_decoded_frames(self):
        eight = encoders.encoder_args('hevc_videotoolbox', ten_bit=False, hw_frames=False)
        self.assertNotIn('-pix_fmt', eight)
        ten = encoders.encoder_args('hevc_videotoolbox', ten_bit=True, hw_frames=False)
        self.assertEqual(ten[ten.index('-pix_fmt') + 1], 'p010le')

    def test_only_videotoolbox_uses_hardware_decode(self):
        for name, profile in encoders.PROFILES.items():
            self.assertEqual(bool(profile['decode_args']), name == 'hevc_videotoolbox', name)

    def test_unknown_encoder_lists_choices(self):
        with self.assertRaisesRegex(RuntimeError, 'libx265'):
            encoders.profile_for('h264_nope')


class PlatformTests(unittest.TestCase):
    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = open(Path(tmp) / 'q.lock', 'a+')
            second = open(Path(tmp) / 'q.lock', 'a+')
            try:
                platform_support.lock_file(first)
                with self.assertRaises(BlockingIOError):
                    platform_support.lock_file(second)
            finally:
                first.close()
                second.close()

    def test_find_tool_prefers_configured_path(self):
        import sys
        self.assertEqual(platform_support.find_tool('nonexistent-tool', sys.executable), sys.executable)
        self.assertIsNone(platform_support.find_tool('definitely-not-installed-xyz'))


if __name__ == '__main__':
    unittest.main()
