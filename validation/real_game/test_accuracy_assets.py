"""Contracts for runtime-only advancement-accuracy media provisioning; no network access."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from validation.real_game import prepare_accuracy_assets as media
from validation.real_game.verify_trace import scenario_for_phase


class AdvancementAccuracyAssetProvisioningTests(unittest.TestCase):
    def _manifest(self, root: Path, payload: bytes = b'fixture-audio') -> Path:
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({
            'schema': 'autoadv-advancement-accuracy-assets-v1',
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


class AdvancementAccuracyScenarioTests(unittest.TestCase):
    def test_accuracy_scene_is_one_continuous_adv_playthrough(self):
        scenario = scenario_for_phase('accuracy')
        self.assertEqual(len(scenario), 24)
        self.assertEqual(scenario[0]['id'], 'room-clean-short')
        self.assertEqual(scenario[-1]['id'], 'quiet-recovery')
        self.assertTrue(all(line['advance_mode'] == 'audio' and not line['use_tts']
                            for line in scenario))
        self.assertEqual(
            [line['scene'] for line in scenario],
            ['quiet_room'] * 3 + ['room_bgm'] * 3 + ['cafe'] * 4 +
            ['street'] * 5 + ['incident'] * 4 + ['machine_room'] * 4 +
            ['quiet_recovery'])
        unvoiced = [line for line in scenario if not line['foreground_voice']]
        self.assertEqual([line['id'] for line in unvoiced], [
            'room-clean-unvoiced', 'room-bgm-unvoiced', 'cafe-crowd-unvoiced',
            'street-background-unvoiced', 'incident-sfx-tail-unvoiced',
            'machine-unvoiced'])
        self.assertTrue(all(line['speaker_family'] == 'unvoiced' for line in unvoiced))
        self.assertFalse(next(line for line in scenario if line['id'] == 'room-clean-unvoiced')
                         ['process_audio_expected'])
        self.assertTrue(all(line['process_audio_expected'] for line in unvoiced[1:]))
        self.assertEqual(sum(bool(line['process_audio_expected']) for line in scenario), 23)
        families = {line['speaker_family'] for line in scenario}
        self.assertTrue({'amitaro', 'amitaro_whisper', 'jvs001', 'jvs002', 'jvs003',
                         'nonlexical'} <= families)

    def test_scene_mix_contracts_cover_real_adv_transitions(self):
        scenario = {line['id']: line for line in scenario_for_phase('accuracy')}
        self.assertEqual(scenario['room-bgm-unvoiced']['expected_mix_channels'], ['music'])
        self.assertEqual(
            scenario['cafe-entry-door']['expected_mix_channels'],
            ['music', 'adv_ambient', 'adv_sfx'])
        self.assertEqual(
            scenario['cafe-crowd-unvoiced']['expected_mix_channels'],
            ['music', 'adv_ambient'])
        self.assertEqual(
            scenario['street-background-unvoiced']['expected_mix_channels'],
            ['music', 'adv_background'])
        self.assertEqual(
            scenario['incident-midvoice-sfx']['expected_mix_channels'],
            ['music', 'adv_sfx'])
        self.assertEqual(
            scenario['incident-sfx-tail-unvoiced']['expected_mix_channels'],
            ['music', 'adv_sfx'])
        self.assertEqual(
            scenario['machine-transition-jvs003']['expected_mix_channels'],
            ['music', 'adv_ambient'])
        self.assertEqual(
            scenario['machine-fading-bgm-whisper']['expected_mix_channels'],
            ['music', 'adv_ambient'])
        self.assertEqual(scenario['machine-unvoiced']['expected_mix_channels'], ['adv_ambient'])
        self.assertEqual(scenario['quiet-recovery']['expected_mix_channels'], [])
        self.assertEqual(set(scenario['quiet-recovery']['forbidden_mix_channels']),
                         {'music', 'adv_ambient', 'adv_background', 'adv_sfx'})

    def test_fixture_presents_all_accuracy_cases_in_order_without_scene_restarts(self):
        script = (media.HERE / 'renpy/game/script.rpy').read_text(encoding='utf-8')
        block = script[script.index('label advancement_accuracy:'):
                       script.index('label standard_tts:')]
        self.assertNotIn('jump ', block)
        self.assertIn('register_channel("adv_ambient", "sfx"', script)
        self.assertIn('register_channel("adv_background", "sfx"', script)
        self.assertIn('register_channel("adv_sfx", "sfx"', script)
        self.assertIn('show screen adv_delayed_sfx(0.8', block)
        self.assertIn('play adv_sfx "audio/runtime/sfx/DOORWood_FrontDoorOpen_OS_01.ogg"', block)
        self.assertIn('play adv_ambient "audio/runtime/ambient/CRWDConv_Restaurant_LOOP_01.ogg"', block)
        self.assertIn('play adv_background "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav" loop', block)
        self.assertIn('play adv_ambient "audio/runtime/ambient/AMBInd_RoomToneMachineRoom_LOOP_01.ogg"', block)
        cursor = 0
        for line in scenario_for_phase('accuracy'):
            needle = '"' + line['text'] + '"'
            cursor = block.index(needle, cursor) + len(needle)

    def test_mid_voice_sfx_is_long_enough_to_cross_the_next_dialogue_boundary(self):
        assets = {asset['id']: asset for asset in media._load_manifest()['assets']}
        garage = assets['meta-garage-door-close']
        voice_manifest = json.loads((media.HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
        voice = next(sample for sample in voice_manifest['samples']
                     if sample['id'] == 'normal-030')
        delay = 0.8
        self.assertLess(delay, voice['duration_s'])
        self.assertGreater(delay + garage['duration_s'], voice['duration_s'] + 1.0)
        self.assertEqual(garage['sha256'],
                         'ee6991718012747c804d89db537ce5b4bb2f41be82e912193c9647cfb3fe5528')

    def test_accuracy_scene_uses_cached_jvs_speech_for_japanese_lexical_background(self):
        script = (media.HERE / 'renpy/game/script.rpy').read_text(encoding='utf-8')
        self.assertNotIn('jvs_music_ver1', script)
        self.assertIn(
            'play adv_background "audio/runtime/voice/jvs001_VOICEACTRESS100_001.wav" loop',
            script)
        voiced = script.index('a "彼のあだ名は言い得て妙だよね。"')
        interfered = script.index('p "この場面の主人公にも音声は付いていない。"', voiced)
        stop = script.index('stop adv_background fadeout 0.2', interfered)
        self.assertLess(voiced, interfered)
        self.assertLess(interfered, stop)

    def test_every_external_foreground_file_is_backed_by_the_runtime_manifest(self):
        manifest_paths = {asset['runtime_path'].removeprefix('game/')
                          for asset in media._load_manifest()['assets']}
        external = {line['foreground_file'] for line in scenario_for_phase('accuracy')
                    if line.get('foreground_file', '').startswith('audio/runtime/')}
        self.assertTrue(external)
        self.assertTrue(external <= manifest_paths)


if __name__ == '__main__':
    unittest.main()
