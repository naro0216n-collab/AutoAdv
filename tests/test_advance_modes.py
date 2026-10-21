"""Contract tests for independent audio choice, two progress modes, state and persistence."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autoadv.conversation import Controller, Target
from autoadv.model import (AdvanceMode, Profile, HookSelection, ReplacementRule,
                           count_reading_characters, interpret)
from autoadv.storage import Catalog, CommonSettings, load, save


SOURCE = SimpleNamespace(hook_code='H', context=1, context2=2)


def make_profile(mode=AdvanceMode.TEXT, tts=False, fallback=False):
    return Profile('p', 'game', hooks=[HookSelection('H', 1, 2)], advance_mode=mode,
                   default_tts=tts, default_voice='v0', uncertain_text_fallback=fallback,
                   reading_ms=350, per_character_wait_ms=180)


class ReadingCharacters(unittest.TestCase):
    def test_unicode_and_ruby_contract(self):
        for raw, count in (('「おはよう！」', 4), ('「……」', 0),
                           ('　（！？）🎉\n', 0), ('花(はな)は赤い', 4),
                           ('かなー', 3), ('ー', 0), ('AB 12.', 4),
                           ('か\u3099', 1), ('東京(とうきょう)です', 4)):
            with self.subTest(raw=raw):
                self.assertEqual(count_reading_characters(raw), count)

    def test_effective_body_ignores_speaker_and_tts_changes(self):
        p = make_profile(tts=True)
        p.replacements = [ReplacementRule('こんにちは', 'はろー', target='tts')]
        u = interpret('京「こんにちは！」', p, session=1, line=1, source='x', sequence=1)
        self.assertEqual(u.character_count, 5)
        self.assertEqual(u.text_wait_ms, 900)
        self.assertEqual(u.speaker, '京')
        self.assertEqual(u.reading, 'はろー!')
        self.assertTrue(u.use_tts)
        self.assertEqual(interpret('「……」', p, session=1, line=2, source='x', sequence=2).text_wait_ms, 350)

    def test_speaker_tts_independent_of_advance_mode(self):
        p = make_profile(AdvanceMode.AUDIO, True, True)
        p.speaker_tts = {'京': False}
        p.speaker_voices = {'京': 'v1'}
        u = interpret('京「はい」', p, session=1, line=1, source='x', sequence=1)
        self.assertFalse(u.use_tts)
        self.assertEqual(u.voice, 'v1')
        self.assertEqual(u.advance_mode, AdvanceMode.AUDIO)
        self.assertTrue(u.uncertain_text_fallback)


class ControllerModes(unittest.TestCase):
    def begin(self, mode, tts, fallback=False):
        self.sent = []
        self.ctrl = Controller(lambda target, key: self.sent.append(key), lambda target: True)
        self.ctrl.begin(make_profile(mode, tts, fallback), Target(10, 20), 'generation', 1)
        self.ctrl.permit(True)
        self.token = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                       source=SOURCE, text='「音声テスト」', pid=10)

    def done(self, event, success=True):
        return self.ctrl.presentation_event(self.token, event, success=success)

    def test_text_tts_requires_both_in_either_order(self):
        for first, second in (('tts_finished', 'text_elapsed'), ('text_elapsed', 'tts_finished')):
            with self.subTest(first=first):
                self.begin(AdvanceMode.TEXT, True)
                self.assertFalse(self.done(first))
                self.assertFalse(self.ctrl.try_advance())
                self.assertTrue(self.done(second))
                self.assertEqual(self.ctrl.presentation_reason, 'tts_and_text')
                self.assertTrue(self.ctrl.try_advance())
                self.assertFalse(self.done(second))
                self.assertFalse(self.ctrl.try_advance())
                self.assertEqual(len(self.sent), 1)

    def test_text_non_tts_does_not_accept_asr_as_completion(self):
        self.begin(AdvanceMode.TEXT, False)
        self.assertFalse(self.done('native_finished'))
        self.assertFalse(self.done('tts_finished'))
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('text_elapsed'))
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_tts_ignores_native_and_fallback(self):
        self.begin(AdvanceMode.AUDIO, True, True)
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertFalse(self.done('native_finished'))
        self.assertFalse(self.done('text_elapsed'))
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('tts_finished'))
        self.assertEqual(self.ctrl.presentation_reason, 'tts')
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_without_tts_confirmed_native_end(self):
        self.begin(AdvanceMode.AUDIO, False)
        self.assertFalse(self.done('native_started'))
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('native_finished'))
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_fallback_requires_explicit_optin_and_no_positive_playing(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertFalse(self.ctrl.try_advance())
        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertFalse(self.done('native_started'))
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('native_quiet'))
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_fallback_without_sound_is_not_evidence_of_voice_absence(self):
        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertTrue(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')
        self.assertFalse(self.done('native_finished'))
        self.assertTrue(self.ctrl.try_advance())

    def test_failure_pause_and_stale_token_cannot_send(self):
        self.begin(AdvanceMode.AUDIO, True)
        self.assertFalse(self.done('tts_finished', success=False))
        self.assertEqual(self.ctrl.reason, 'tts_playback_failed')
        self.assertFalse(self.ctrl.try_advance())
        old = self.token
        self.ctrl.begin(make_profile(), Target(11, 22), 'second', 1)
        self.assertFalse(self.ctrl.presentation_event(old, 'tts_finished'))
        self.assertFalse(self.ctrl.try_advance())

    def test_settings_mutation_does_not_rewrite_active_utterance(self):
        p = make_profile(AdvanceMode.TEXT, tts=True)
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        self.ctrl.observe(generation='generation', connection=1, sequence=1,
                          source=SOURCE, text='「こんにちは」', pid=10)
        original = self.ctrl.utterance
        p.advance_mode = AdvanceMode.AUDIO
        p.default_tts = False
        p.reading_ms = 3000
        p.per_character_wait_ms = 500
        self.assertEqual(self.ctrl.utterance, original)
        self.assertEqual(original.advance_mode, AdvanceMode.TEXT)
        self.assertEqual(original.text_wait_ms, 900)
        self.assertTrue(original.use_tts)


class StorageModes(unittest.TestCase):
    def test_common_and_game_overrides_roundtrip(self):
        catalog = Catalog(common=CommonSettings(advance_mode=AdvanceMode.AUDIO,
                uncertain_text_fallback=True, default_tts=True, reading_ms=880,
                per_character_wait_ms=210))
        game = catalog.create_game('Example')
        stored = game.profiles[game.default_profile]
        stored.inherit_common = ['advance_mode', 'uncertain_text_fallback', 'per_character_wait_ms']
        stored.default_tts = False
        stored.reading_ms = 550
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings-v4.json'
            save(catalog, path)
            restored = load(path)
        p = restored.effective_profile(restored.games[game.id].profiles[stored.id])
        self.assertEqual(p.advance_mode, AdvanceMode.AUDIO)
        self.assertTrue(p.uncertain_text_fallback)
        self.assertEqual(p.per_character_wait_ms, 210)
        self.assertEqual(p.reading_ms, 550)
        self.assertFalse(p.default_tts)

    def test_v3_remains_unmodified_and_ambiguous_modes_need_confirmation(self):
        previous = Catalog()
        game = previous.create_game('Saved')
        # v3 is represented faithfully as an input file; this is not a v3 runtime.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings-v4.json'
            save(previous, path)
            raw = json.loads(path.read_text('utf-8'))
            raw['schema_version'] = 3
            raw['common'].pop('advance_mode')
            raw['common'].pop('uncertain_text_fallback')
            raw['common'].pop('default_tts')
            raw['common'].pop('per_character_wait_ms')
            raw['common']['default_mode'] = 'auto'
            raw['common']['reading_ms'] = 0
            for item in raw['games'].values():
                for p in item['profiles'].values():
                    p.pop('advance_mode')
                    p.pop('uncertain_text_fallback')
                    p.pop('default_tts')
                    p.pop('speaker_tts')
                    p.pop('per_character_wait_ms', None)
                    p['default_mode'] = 'auto'
                    p['speaker_modes'] = {'主人公': 'tts'}
            original = json.dumps(raw, ensure_ascii=False, indent=2)
            v3 = Path(tmp) / 'settings-v3.json'
            v3.write_text(original, 'utf-8')
            path.unlink()
            migrated = load(path)
            self.assertEqual(v3.read_text('utf-8'), original)
            self.assertTrue(migrated.migration_review)
            self.assertEqual(migrated.common.advance_mode, AdvanceMode.TEXT)
            self.assertFalse(migrated.common.uncertain_text_fallback)
            self.assertEqual(migrated.common.reading_ms, 700)
            imported = next(iter(next(iter(migrated.games.values())).profiles.values()))
            self.assertTrue(imported.speaker_tts['主人公'])
            save(migrated, path)
            self.assertEqual(v3.read_text('utf-8'), original)
            self.assertEqual(load(path).migration_review, [])


if __name__ == '__main__':
    unittest.main()
