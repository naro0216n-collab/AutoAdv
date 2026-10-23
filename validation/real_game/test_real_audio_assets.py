"""Contracts for runtime-only RealAudio media provisioning; no network access."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from validation.real_game import prepare_real_audio_assets as media
from validation.real_game.verify_trace import scenario_for_phase


class RealAudioAssetProvisioningTests(unittest.TestCase):
    def _manifest(self, root: Path, payload: bytes = b'fixture-audio') -> Path:
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({
            'schema': 'autoadv-real-audio-assets-v1',
            'assets': [{
                'id': 'sample', 'kind': 'foreground_voice', 'filename': 'sample.wav',
                'runtime_path': 'game/audio/runtime/voice/sample.wav',
                'sha256': hashlib.sha256(payload).hexdigest(), 'size_bytes': len(payload),
                'max_bytes': 1024, 'source_page': 'https://example.test/source',
                'urls': ['https://example.test/sample.wav'], 'license': 'test-only',
                'redistributable': False,
            }],
        }), encoding='utf-8')
        return manifest

    def test_cached_asset_is_reused_without_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            cache = root / 'cache'
            cache.mkdir()
            (cache / 'sample.wav').write_bytes(b'fixture-audio')
            downloader = Mock(side_effect=AssertionError('cache hit must not download'))
            result = media.prepare(cache, manifest_path=manifest, downloader=downloader)
            self.assertEqual(result['assets'][0]['source'], 'cache')
            downloader.assert_not_called()

    def test_missing_asset_downloads_once_then_becomes_cache_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            cache = root / 'cache'
            calls = []
            def downloader(url, destination, max_bytes):
                calls.append((url, max_bytes))
                destination.write_bytes(b'fixture-audio')
            first = media.prepare(cache, manifest_path=manifest, downloader=downloader)
            second = media.prepare(cache, manifest_path=manifest, downloader=downloader)
            self.assertEqual(first['assets'][0]['source'], 'downloaded')
            self.assertEqual(second['assets'][0]['source'], 'cache')
            self.assertEqual(len(calls), 1)

    def test_bad_existing_cache_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            cache = root / 'cache'
            cache.mkdir()
            (cache / 'sample.wav').write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'size mismatch|hash mismatch'):
                media.prepare(cache, manifest_path=manifest)

    def test_google_drive_confirmation_form_can_be_followed_without_browser_automation(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / 'drive.html'
            page.write_text('''<!doctype html><html><body>
                <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
                  <input type="hidden" name="id" value="abc123">
                  <input type="hidden" name="export" value="download">
                  <input type="hidden" name="confirm" value="t">
                  <input type="hidden" name="uuid" value="fixed-token">
                </form></body></html>''', encoding='utf-8')
            action, method, fields = media._drive_confirmation(page)
            self.assertEqual(action, 'https://drive.usercontent.google.com/download')
            self.assertEqual(method, 'get')
            self.assertEqual(fields['id'], 'abc123')
            self.assertEqual(fields['uuid'], 'fixed-token')

    def test_download_headers_are_browser_compatible_and_source_specific(self):
        drive = media._request_headers('https://drive.usercontent.google.com/download?id=x')
        oga = media._request_headers('https://opengameart.org/sites/default/files/a.ogg')
        github = media._request_headers('https://raw.githubusercontent.com/a/b/c/a.ogg')
        self.assertIn('Mozilla/5.0', drive['User-Agent'])
        self.assertEqual(drive['Referer'], 'https://drive.google.com/')
        self.assertEqual(oga['Referer'], 'https://opengameart.org/')
        self.assertEqual(github['Referer'], 'https://github.com/')

    def test_hydration_copies_verified_cache_only_into_runtime_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            cache = root / 'cache'
            cache.mkdir()
            (cache / 'sample.wav').write_bytes(b'fixture-audio')
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / 'AutoAdvValidationGame.exe').write_bytes(b'MZ')
            result = media.hydrate(runtime, cache, manifest_path=manifest)
            staged = runtime / 'game/audio/runtime/voice/sample.wav'
            self.assertEqual(staged.read_bytes(), b'fixture-audio')
            self.assertEqual(result['assets'][0]['runtime_path'],
                             'game/audio/runtime/voice/sample.wav')

    def test_real_manifest_uses_small_individual_downloads_and_no_runtime_media_is_committed(self):
        info = media._load_manifest()
        self.assertEqual(len(info['assets']), 9)
        self.assertTrue(all(len(asset['urls']) >= 1 for asset in info['assets']))
        self.assertTrue(all(int(asset['max_bytes']) <= 16 * 1024 * 1024
                            for asset in info['assets']))
        self.assertTrue(any(asset['redistributable'] is False for asset in info['assets']))
        self.assertFalse((media.HERE / 'renpy/game/audio/runtime').exists())
        # Three independent JVS speaker samples are individual sub-megabyte files,
        # not members extracted from the 3.5 GB corpus archive.
        voices = [asset for asset in info['assets'] if asset['id'].startswith('jvs00')]
        self.assertEqual(len(voices), 3)
        self.assertTrue(all(asset['size_bytes'] < 1024 * 1024 for asset in voices))
        self.assertTrue(all('.zip' not in url.lower() for asset in voices for url in asset['urls']))
        self.assertNotIn('jvs-music-jvs001-sample', {asset['id'] for asset in info['assets']})
        drive_assets = [asset for asset in info['assets']
                        if any('drive.' in url for url in asset['urls'])]
        self.assertEqual({asset['id'] for asset in drive_assets},
                         {'jvs001-sample', 'jvs002-sample', 'jvs003-sample'})
        self.assertTrue(all(asset['size_bytes'] < 1024 * 1024 for asset in drive_assets))


class RealAudioScenarioTests(unittest.TestCase):
    def test_native_scene_spans_speakers_and_real_game_mix_classes(self):
        scenario = scenario_for_phase('native')
        self.assertEqual(len(scenario), 21)
        self.assertEqual(scenario[0]['id'], 'clean-local-short')
        self.assertEqual(scenario[-1]['id'], 'clean-recovery')
        self.assertTrue(all(line['advance_mode'] == 'audio' and not line['use_tts']
                            and line['foreground_voice'] for line in scenario))
        families = {line['speaker_family'] for line in scenario}
        self.assertTrue({'amitaro', 'amitaro_whisper', 'jvs001', 'jvs002', 'jvs003'} <= families)
        environments = {line['environment'] for line in scenario}
        required = {
            'clean', 'instrumental_bgm_soft', 'instrumental_bgm_dense',
            'machine_ambient', 'background_conversation',
            'background_conversation_outlasts_foreground', 'japanese_background_voice',
            'japanese_background_voice_same_text_boundary',
            'transient_sfx_outlasts_short_voice', 'door_sfx',
            'clean_after_complex_mix',
        }
        self.assertTrue(required <= environments)

    def test_native_scene_uses_cached_jvs_speech_for_japanese_lexical_background(self):
        script = (media.HERE / 'renpy/game/script.rpy').read_text(encoding='utf-8')
        self.assertNotIn('jvs_music_ver1', script)
        self.assertIn(
            'play sound "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav" loop',
            script)

    def test_every_external_foreground_file_is_backed_by_the_runtime_manifest(self):
        manifest_paths = {asset['runtime_path'].removeprefix('game/')
                          for asset in media._load_manifest()['assets']}
        external = {line['foreground_file'] for line in scenario_for_phase('native')
                    if line['foreground_file'].startswith('audio/runtime/')}
        self.assertTrue(external)
        self.assertTrue(external <= manifest_paths)


if __name__ == '__main__':
    unittest.main()
