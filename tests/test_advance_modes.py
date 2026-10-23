"""Contract tests for independent audio choice, two progress modes, state and persistence."""
import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autoadv.conversation import Controller, Target
from autoadv.model import (AdvanceMode, Profile, HookSelection, OCRSettings, ReplacementRule,
                           count_reading_characters, interpret, matching_structure_rule, StructureRule)
from autoadv.storage import Catalog, CommonSettings, load, save


SOURCE = SimpleNamespace(hook_code='H', context=1, context2=2)


def make_profile(mode=AdvanceMode.TEXT, tts=False, fallback=False):
    return Profile('p', 'game', hooks=[HookSelection('H', 1, 2)], body_source='H|1|2', advance_mode=mode,
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

    def test_prose_colon_and_url_are_not_speaker_delimiters(self):
        profile = make_profile()
        for raw in (
            'AutoAdv検証用ゲームです。使用音声は、あみたろの声素材工房（https://amitaro.net/）によるものです。',
            'これは説明です：https://example.org/voice',
            '彼は「京：おはよう」と書いた。',
        ):
            with self.subTest(raw=raw):
                utterance = interpret(raw, profile, session=1, line=1, source='hook', sequence=1)
                self.assertEqual(utterance.speaker, '')
                self.assertEqual(utterance.text, __import__('unicodedata').normalize('NFKC', raw))
        explicit = interpret('京：おはようございます。', profile,
                             session=1, line=1, source='hook', sequence=1)
        self.assertEqual((explicit.speaker, explicit.text), ('京', 'おはようございます。'))

    def test_rule_preview_trace_is_the_actual_interpretation_path(self):
        p = make_profile(tts=True)
        p.source_rules['hook'] = [StructureRule(r'(?P<body>notmatched)', enabled=False),
                                  StructureRule(r'^\s*(?P<speaker>[^「」]+)「(?P<body>.*?)」$')]
        p.name_readings['京'] = 'みやこ'
        p.replacements = [ReplacementRule('みやこ', 'ミヤコ', target='tts'),
                          ReplacementRule('京', '京都', target='asr')]
        p.speaker_tts['大和'] = False
        p.speaker_voices['大和'] = 'v2'
        raw = '大和「京さん」'
        match, index, _ = matching_structure_rule(raw, p.source_rules['hook'])
        self.assertIsNotNone(match)
        self.assertEqual(index, 1)
        trace = []
        u = interpret(raw, p, session=1, line=1, source='hook', sequence=1, trace=trace)
        without_trace = interpret(raw, p, session=1, line=1, source='hook', sequence=1)
        self.assertEqual(u, without_trace)
        self.assertEqual((u.speaker, u.text, u.reading, u.match_text, u.use_tts, u.voice),
                         ('大和', '京さん', 'ミヤコさん', '京都さん', False, 'v2'))
        self.assertEqual(trace, ['抽出: 優先順位 2', '読み: 京 → みやこ',
                                 '置換 1: TTS文を変更', '置換 2: ASR文を変更'])

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

    def test_staged_speaker_voice_change_applies_only_to_the_next_utterance(self):
        profile = make_profile(AdvanceMode.TEXT, True)
        profile.speaker_tts['京'] = True
        profile.speaker_voices['京'] = 'voice-1'
        ctrl = Controller(lambda *_: None, lambda _: True)
        ctrl.begin(profile, Target(10, 20), 'generation', 1)
        first = ctrl.observe(generation='generation', connection=1, sequence=1,
                             source=SOURCE, text='京「一つ目」', pid=10)
        self.assertIsNotNone(first)
        self.assertEqual(ctrl.utterance.voice, 'voice-1')

        updated = deepcopy(profile)
        updated.speaker_voices['京'] = 'voice-2'
        ctrl.stage_profile(updated)
        self.assertEqual(ctrl.utterance.voice, 'voice-1')

        second = ctrl.observe(generation='generation', connection=1, sequence=2,
                              source=SOURCE, text='京「二つ目」', pid=10)
        self.assertIsNotNone(second)
        self.assertEqual(ctrl.utterance.voice, 'voice-2')

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
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('native_finished'))
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_fallback_requires_explicit_optin(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertFalse(self.ctrl.try_advance())
        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertTrue(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_fallback_without_sound_is_not_evidence_of_voice_absence(self):
        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertTrue(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')
        self.assertFalse(self.done('native_finished'))
        self.assertTrue(self.ctrl.try_advance())

    def test_uncertain_audio_without_opt_in_hands_control_back_at_reading_deadline(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.pause_reason, 'native_inconclusive_hold')
        self.assertTrue(self.ctrl.authorized)
        self.assertIsNone(self.ctrl.presentation_reason)
        self.assertFalse(self.ctrl.try_advance())

    def test_late_native_finish_resolves_only_the_inconclusive_native_hold(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.pause_reason, 'native_inconclusive_hold')

        self.assertTrue(self.done('native_finished'))
        self.assertIsNone(self.ctrl.pause_reason)
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertTrue(self.ctrl.try_advance())

    def test_late_native_finish_does_not_clear_an_unrelated_safety_hold(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.ctrl.pause('host_changed')

        self.assertTrue(self.done('native_finished'))
        self.assertEqual(self.ctrl.pause_reason, 'host_changed')
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertFalse(self.ctrl.try_advance())

    def test_native_failure_policy_is_owned_by_controller(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('native_failed'))
        self.assertEqual(self.ctrl.pause_reason, 'native_audio_failure')
        self.assertFalse(self.ctrl.try_advance())

        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertFalse(self.done('native_failed'))
        self.assertIsNone(self.ctrl.pause_reason)
        self.assertTrue(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')

    def test_existing_runtime_hold_is_not_replaced_by_later_unrelated_failure(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.ctrl.pause('target_unavailable')
        self.assertFalse(self.done('native_failed'))
        self.assertEqual(self.ctrl.pause_reason, 'target_unavailable')

    def test_runtime_hold_survives_auto_off_on_cycle(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.ctrl.permit(False)
        self.assertFalse(self.ctrl.authorized)
        self.assertFalse(self.done('fallback_elapsed'))
        self.assertEqual(self.ctrl.pause_reason, 'native_inconclusive_hold')

        self.ctrl.permit(True)
        self.assertTrue(self.ctrl.authorized)
        self.assertEqual(self.ctrl.pause_reason, 'native_inconclusive_hold')
        self.assertFalse(self.ctrl.try_advance())

    def test_tts_failure_remains_actionable_while_auto_is_off(self):
        self.begin(AdvanceMode.TEXT, True)
        self.ctrl.permit(False)
        self.assertFalse(self.done('tts_finished', success=False))
        self.assertEqual(self.ctrl.pause_reason, 'tts_playback_failed')
        self.ctrl.permit(True)
        self.assertEqual(self.ctrl.pause_reason, 'tts_playback_failed')
        self.assertFalse(self.ctrl.try_advance())

    def test_failure_pause_and_stale_token_cannot_send(self):
        self.begin(AdvanceMode.AUDIO, True)
        self.assertFalse(self.done('tts_finished', success=False))
        self.assertEqual(self.ctrl.pause_reason, 'tts_playback_failed')
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

    def test_staged_profile_applies_only_at_next_utterance_boundary(self):
        p = make_profile(AdvanceMode.TEXT, tts=False)
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        first = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                  source=SOURCE, text='「一行目」', pid=10)
        original = self.ctrl.utterance
        updated = make_profile(AdvanceMode.AUDIO, tts=False)
        updated.id = p.id
        updated.hooks = list(p.hooks)
        updated.input_key = 99
        self.ctrl.stage_profile(updated)
        self.assertEqual(self.ctrl.utterance, original)
        self.assertEqual(self.ctrl.profile.input_key, p.input_key)
        self.ctrl.permit(True)
        self.ctrl.presentation_event(first, 'text_elapsed')
        self.assertTrue(self.ctrl.try_advance())
        second = self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                   source=SOURCE, text='「二行目」', pid=10)
        self.assertIsNotNone(second)
        self.assertEqual(self.ctrl.profile.input_key, 99)
        self.assertEqual(self.ctrl.utterance.advance_mode, AdvanceMode.AUDIO)
        self.assertFalse(self.ctrl.utterance.use_tts)
        self.assertIsNone(self.ctrl.pending_profile)

    def test_staged_profile_rejects_capture_ownership_changes(self):
        p = make_profile()
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        changed = make_profile()
        changed.id = p.id
        changed.hooks = [HookSelection('OTHER', 9, 10)]
        with self.assertRaises(ValueError):
            self.ctrl.stage_profile(changed)

    def test_staged_profile_waits_through_same_utterance_duplicate(self):
        p = make_profile()
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        self.ctrl.observe(generation='generation', connection=1, sequence=1,
                          source=SOURCE, text='「同じ台詞」', pid=10)
        updated = make_profile(AdvanceMode.AUDIO)
        updated.id = p.id
        updated.hooks = list(p.hooks)
        self.ctrl.stage_profile(updated)
        self.assertIsNone(self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                            source=SOURCE, text='「同じ台詞」', pid=10))
        self.assertEqual(self.ctrl.profile.advance_mode, AdvanceMode.TEXT)
        self.assertIsNotNone(self.ctrl.pending_profile)

    def test_staged_repeat_policy_decides_next_identical_line(self):
        p = make_profile()
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        first = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                  source=SOURCE, text='「繰り返し」', pid=10)
        updated = make_profile()
        updated.id = p.id
        updated.hooks = list(p.hooks)
        updated.repeat_rule = 'each_after_input'
        self.ctrl.stage_profile(updated)
        self.ctrl.permit(True)
        self.ctrl.presentation_event(first, 'text_elapsed')
        self.assertTrue(self.ctrl.try_advance())
        second = self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                   source=SOURCE, text='「繰り返し」', pid=10)
        self.assertIsNotNone(second)
        self.assertEqual(self.ctrl.profile.repeat_rule, 'each_after_input')

    def test_unsent_new_hook_is_external_transition_without_disabling_auto(self):
        p = make_profile(AdvanceMode.AUDIO, tts=False)
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        first = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                  source=SOURCE, text='「一行目」', pid=10, received_ns=1_000)
        self.ctrl.permit(True)
        second = self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                   source=SOURCE, text='「二行目」', pid=10, received_ns=2_500)
        self.assertIsNotNone(second)
        self.assertEqual(second.line, first.line + 1)
        self.assertTrue(self.ctrl.authorized)
        self.assertEqual(self.ctrl.take_external_transition(second), 2_500)
        self.assertEqual(self.ctrl.take_external_transition(second), 0)
        self.assertFalse(self.ctrl.presentation_event(first, 'native_finished'))

    def test_unsent_identical_hook_is_not_guessed_as_external_transition(self):
        p = make_profile(AdvanceMode.TEXT, tts=False)
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        first = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                  source=SOURCE, text='「同じ台詞」', pid=10, received_ns=1_000)
        self.ctrl.permit(True)
        self.assertIsNone(self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                            source=SOURCE, text='「同じ台詞」', pid=10, received_ns=2_500))
        self.assertEqual(self.ctrl.token, first)
        self.assertTrue(self.ctrl.authorized)

    def test_staged_profile_rejects_active_ocr_capture_changes(self):
        p = make_profile()
        p.source = 'ocr'
        p.ocr = OCRSettings('tesseract', 'jpn', (0, 0, 640, 180), stable_samples=2)
        self.ctrl = Controller(lambda *_: None, lambda *_: True)
        self.ctrl.begin(p, Target(10, 20), 'ocr', 1)
        changed = make_profile()
        changed.id = p.id
        changed.source = 'ocr'
        changed.hooks = list(p.hooks)
        changed.ocr = OCRSettings('tesseract', 'jpn', (0, 0, 800, 240), stable_samples=2)
        with self.assertRaises(ValueError):
            self.ctrl.stage_profile(changed)


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
