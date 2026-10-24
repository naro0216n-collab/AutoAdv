"""Contracts required for a valid runtime accuracy measurement."""
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

    def test_verified_cache_is_reused_and_invalid_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            cache = root / 'cache'
            cache.mkdir()
            cached = cache / 'sample.wav'
            cached.write_bytes(b'fixture-audio')
            downloader = Mock(side_effect=AssertionError('verified cache must not download'))
            result = media.prepare(cache, manifest_path=manifest, downloader=downloader)
            self.assertEqual(result['assets'][0]['source'], 'cache')
            downloader.assert_not_called()

            cached.write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'size mismatch|hash mismatch'):
                media.prepare(cache, manifest_path=manifest)

    def test_missing_asset_is_acquired_once_then_reused(self):
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

    def test_hydration_uses_only_verified_cached_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            cache = root / 'cache'
            cache.mkdir()
            (cache / 'sample.wav').write_bytes(b'fixture-audio')
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / 'AutoAdvValidationGame.exe').write_bytes(b'MZ')
            media.hydrate(runtime, cache, manifest_path=manifest)
            self.assertEqual(
                (runtime / 'game/audio/runtime/voice/sample.wav').read_bytes(),
                b'fixture-audio')


class AdvancementAccuracyScenarioTests(unittest.TestCase):
    def test_standard_measurement_has_the_canonical_voiced_unvoiced_and_mix_coverage(self):
        scenario = scenario_for_phase('accuracy')
        self.assertEqual(len(scenario), 24)
        self.assertEqual(sum(bool(line['foreground_voice']) for line in scenario), 18)
        self.assertEqual(sum(not bool(line['foreground_voice']) for line in scenario), 6)
        self.assertTrue(all(line['advance_mode'] == 'audio' and not line['use_tts']
                            for line in scenario))
        self.assertTrue(any(line['expected_mix_channels'] for line in scenario))
        self.assertTrue(any('adv_background' in line['expected_mix_channels'] for line in scenario))
        self.assertTrue(any('adv_sfx' in line['expected_mix_channels'] for line in scenario))
        self.assertTrue(any(line['speaker_family'] == 'nonlexical' for line in scenario))


if __name__ == '__main__':
    unittest.main()
