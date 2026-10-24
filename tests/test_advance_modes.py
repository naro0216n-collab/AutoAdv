"""Contract tests for independent audio choice, two progress modes, state and persistence."""
import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.g2p_support import TEST_G2P, FakeG2P

from autoadv.conversation import Controller, Target
from autoadv.model import (AdvanceMode, Profile, HookSelection, OCRSettings, ReplacementRule,
                           interpret, matching_structure_rule, StructureRule)
from autoadv.storage import Catalog, CommonSettings, load, save


SOURCE = SimpleNamespace(hook_code='H', context=1, context2=2)


def make_profile(mode=AdvanceMode.TEXT, tts=False, fallback=False):
    return Profile('p', 'game', hooks=[HookSelection('H', 1, 2)], body_source='H|1|2', advance_mode=mode,
                   default_tts=tts, default_voice='v0', uncertain_text_fallback=fallback,
                   reading_ms=350, per_reading_unit_wait_ms=180)


class ReadingTiming(unittest.TestCase):
    def test_primary_pronunciation_uses_mora_like_units_not_display_characters(self):
        g2p = FakeG2P({
            '学校': ('g', 'a', 'cl', 'k', 'o', 'o'),
            '愛': ('a', 'i'),
        })
        p = make_profile()
        school = interpret('「学校」', p, session=1, line=1, source='x', sequence=1, g2p=g2p)
        love = interpret('「愛」', p, session=1, line=2, source='x', sequence=2, g2p=g2p)
        punctuation = interpret('「……」', p, session=1, line=3, source='x', sequence=3, g2p=g2p)
        self.assertEqual((school.reading_unit_count, school.text_wait_ms), (4, 720))
        self.assertEqual((love.reading_unit_count, love.text_wait_ms), (2, 360))
        self.assertEqual((punctuation.reading_unit_count, punctuation.text_wait_ms), (0, 350))

    def test_tts_only_changes_do_not_rewrite_pronunciation_timing(self):
        p = make_profile(tts=True)
        p.replacements = [ReplacementRule('こんにちは', 'はろー', target='tts')]
        u = interpret('京「こんにちは！」', p, session=1, line=1, source='x', sequence=1, g2p=TEST_G2P)
        self.assertEqual(u.speech.resolved_text, 'こんにちは！')
        self.assertEqual(u.reading_unit_count, 5)
        self.assertEqual(u.text_wait_ms, 900)
        self.assertEqual(u.speaker, '京')
        self.assertEqual(u.speech.tts_text, 'はろー！')
        self.assertTrue(u.use_tts)

    def test_prose_colon_and_url_are_not_speaker_delimiters(self):
        profile = make_profile()
        for raw in (
            'AutoAdv検証用ゲームです。使用音声は、あみたろの声素材工房（https://amitaro.net/）によるものです。',
            'これは説明です：https://example.org/voice',
            '彼は「京：おはよう」と書いた。',
        ):
            with self.subTest(raw=raw):
                utterance = interpret(raw, profile, session=1, line=1, source='hook', sequence=1, g2p=TEST_G2P)
                self.assertEqual(utterance.speaker, '')
                self.assertEqual(utterance.text, raw)
        explicit = interpret('京：おはようございます。', profile,
                             session=1, line=1, source='hook', sequence=1, g2p=TEST_G2P)
        self.assertEqual((explicit.speaker, explicit.text), ('京', 'おはようございます。'))

    def test_rule_preview_trace_is_the_actual_interpretation_path(self):
        p = make_profile(tts=True)
        p.source_rules['hook'] = [StructureRule(r'(?P<body>notmatched)', enabled=False),
                                  StructureRule(r'^\s*(?P<speaker>[^「」]+)「(?P<body>.*?)」$')]
        p.name_readings['京'] = 'みやこ'
        p.replacements = [ReplacementRule('みやこ', 'ミヤコ', target='tts'),
                          ReplacementRule('みやこ', 'ミヤコ原音', target='match')]
        p.speaker_tts['大和'] = False
        p.speaker_voices['大和'] = 'v2'
        raw = '大和「京さん」'
        match, index, _ = matching_structure_rule(raw, p.source_rules['hook'])
        self.assertIsNotNone(match)
        self.assertEqual(index, 1)
        trace = []
        g2p = FakeG2P(word_spans={'京さん': ((0, 1), (1, 3))})
        u = interpret(raw, p, session=1, line=1, source='hook', sequence=1, g2p=g2p, trace=trace)
        without_trace = interpret(raw, p, session=1, line=1, source='hook', sequence=1, g2p=g2p)
        self.assertEqual(u, without_trace)
        self.assertEqual((u.speaker, u.text, u.speech.resolved_text, u.speech.tts_text,
                          u.speech.alignment_text, u.use_tts, u.voice),
                         ('大和', '京さん', 'みやこさん', 'ミヤコさん', 'ミヤコ原音さん', False, 'v2'))
        self.assertEqual(trace, ['抽出: 優先順位 2', '読み: 京 → みやこ',
                                 '置換 1: TTS文を変更', '置換 2: 原音照合文を変更'])

    def test_registered_readings_apply_only_to_complete_unprotected_word_spans(self):
        cases = (
            ('京(みやこ)', {'みやこ': '別読み'}, {'みやこ': ((0, 3),)}, 'みやこ'),
            ('京(みやこ)様', {'みやこ様': '別読み'}, {'みやこ様': ((0, 3), (3, 4))}, 'みやこ様'),
            ('東京の京', {'京': 'みやこ'}, {'東京の京': ((0, 2), (2, 3), (3, 4))}, '東京のみやこ'),
            ('山田の田', {'田': 'た'}, {'山田の田': ((0, 2), (2, 3), (3, 4))}, '山田のた'),
            ('世界と世', {'世': 'せ'}, {'世界と世': ((0, 2), (2, 3), (3, 4))}, '世界とせ'),
            ('大和京', {'大和京': 'やまとみやこ'}, {'大和京': ((0, 2), (2, 3))}, 'やまとみやこ'),
            ('甲乙', {'甲乙': 'こうおつ'}, {'甲乙': ((0, 1),)}, '甲乙'),
            ('ﾊﾟ京', {'京': 'みやこ'}, {'ﾊﾟ京': ((0, 1), (1, 2))}, 'ﾊﾟみやこ'),
            ('㍑京', {'京': 'みやこ'}, {'㍑京': ((0, 4), (4, 5))}, '㍑みやこ'),
        )
        for body, readings, spans, expected in cases:
            with self.subTest(body=body):
                profile = make_profile()
                profile.name_readings.update(readings)
                utterance = interpret(
                    f'「{body}」', profile, session=1, line=1, source='x', sequence=1,
                    g2p=FakeG2P(word_spans=spans),
                )
                self.assertEqual(utterance.speech.resolved_text, expected)

    def test_both_replacement_updates_shared_pronunciation_base(self):
        p = make_profile()
        p.replacements = [ReplacementRule('東京', 'とうきょう', target='both')]
        g2p = FakeG2P({'とうきょう': ('t', 'o', 'o', 'ky', 'o', 'o')})
        u = interpret('「東京」', p, session=1, line=1, source='x', sequence=1, g2p=g2p)
        self.assertEqual((u.speech.resolved_text, u.speech.tts_text, u.speech.alignment_text),
                         ('とうきょう', 'とうきょう', 'とうきょう'))
        self.assertEqual(u.reading_unit_count, 4)

    def test_speaker_tts_independent_of_advance_mode(self):
        p = make_profile(AdvanceMode.AUDIO, True, True)
        p.speaker_tts = {'京': False}
        p.speaker_voices = {'京': 'v1'}
        u = interpret('京「はい」', p, session=1, line=1, source='x', sequence=1, g2p=TEST_G2P)
        self.assertFalse(u.use_tts)
        self.assertEqual(u.voice, 'v1')
        self.assertEqual(u.advance_mode, AdvanceMode.AUDIO)
        self.assertTrue(u.uncertain_text_fallback)


class ControllerModes(unittest.TestCase):
    def begin(self, mode, tts, fallback=False):
        self.sent = []
        self.ctrl = Controller(lambda target, key: self.sent.append(key), lambda target: True, TEST_G2P)
        self.ctrl.begin(make_profile(mode, tts, fallback), Target(10, 20), 'generation', 1)
        self.ctrl.permit(True)
        self.token = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                       source=SOURCE, text='「音声テスト」', pid=10)

    def done(self, event, success=True):
        return self.ctrl.presentation_event(self.token, event, success=success)

    def native_end(self):
        return self.ctrl.native_acoustic_completed(self.token)

    def test_staged_speaker_voice_change_applies_only_to_the_next_utterance(self):
        profile = make_profile(AdvanceMode.TEXT, True)
        profile.speaker_tts['京'] = True
        profile.speaker_voices['京'] = 'voice-1'
        ctrl = Controller(lambda *_: None, lambda _: True, TEST_G2P)
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

    def test_text_non_tts_does_not_accept_native_alignment_as_completion(self):
        self.begin(AdvanceMode.TEXT, False)
        self.assertFalse(self.native_end())
        self.assertFalse(self.done('tts_finished'))
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('text_elapsed'))
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_tts_ignores_native_and_fallback(self):
        self.begin(AdvanceMode.AUDIO, True, True)
        self.assertFalse(self.done('audio_deadline_elapsed'))
        self.assertFalse(self.native_end())
        self.assertFalse(self.done('text_elapsed'))
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.done('tts_finished'))
        self.assertEqual(self.ctrl.presentation_reason, 'tts')
        self.assertTrue(self.ctrl.try_advance())

    def test_audio_non_tts_completion_state_model(self):
        # Native completion and uncertainty fallback are independent terminal paths.
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertTrue(self.native_end())
        self.assertTrue(self.ctrl.native_acoustic_complete)
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertTrue(self.ctrl.try_advance())

        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertTrue(self.done('audio_deadline_elapsed'))
        self.assertFalse(self.ctrl.native_acoustic_complete)
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')
        self.assertFalse(self.native_end())
        self.assertTrue(self.ctrl.try_advance())

        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('audio_deadline_elapsed'))
        self.assertFalse(self.ctrl.native_acoustic_complete)
        self.assertEqual(self.ctrl.pause_reason, 'native_acoustic_unconfirmed_hold')
        self.assertTrue(self.ctrl.authorized)
        self.assertIsNone(self.ctrl.presentation_reason)
        self.assertFalse(self.ctrl.try_advance())
        self.assertTrue(self.native_end())
        self.assertIsNone(self.ctrl.pause_reason)
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertTrue(self.ctrl.try_advance())

    def test_late_native_finish_does_not_clear_an_unrelated_safety_hold(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.ctrl.pause('host_changed')

        self.assertTrue(self.native_end())
        self.assertEqual(self.ctrl.pause_reason, 'host_changed')
        self.assertEqual(self.ctrl.presentation_reason, 'native')
        self.assertFalse(self.ctrl.try_advance())

    def test_native_failure_respects_fallback_policy_and_existing_holds(self):
        self.begin(AdvanceMode.AUDIO, False, False)
        self.assertFalse(self.done('native_failed'))
        self.assertEqual(self.ctrl.pause_reason, 'native_audio_failure')
        self.assertFalse(self.ctrl.try_advance())

        self.begin(AdvanceMode.AUDIO, False, True)
        self.assertFalse(self.done('native_failed'))
        self.assertIsNone(self.ctrl.pause_reason)
        self.assertTrue(self.done('audio_deadline_elapsed'))
        self.assertEqual(self.ctrl.presentation_reason, 'uncertain_text_fallback')

        self.begin(AdvanceMode.AUDIO, False, False)
        self.ctrl.pause('target_unavailable')
        self.assertFalse(self.done('native_failed'))
        self.assertEqual(self.ctrl.pause_reason, 'target_unavailable')

    def test_runtime_holds_are_independent_of_user_auto_authorization(self):
        scenarios = (
            (AdvanceMode.AUDIO, False, 'audio_deadline_elapsed', True,
             'native_acoustic_unconfirmed_hold'),
            (AdvanceMode.TEXT, True, 'tts_finished', False, 'tts_playback_failed'),
        )
        for mode, tts, event, success, expected_hold in scenarios:
            with self.subTest(event=event):
                self.begin(mode, tts, False)
                self.ctrl.permit(False)
                self.assertFalse(self.ctrl.authorized)
                self.assertFalse(self.done(event, success=success))
                self.assertEqual(self.ctrl.pause_reason, expected_hold)
                self.ctrl.permit(True)
                self.assertTrue(self.ctrl.authorized)
                self.assertEqual(self.ctrl.pause_reason, expected_hold)
                self.assertFalse(self.ctrl.try_advance())

    def test_settings_mutation_does_not_rewrite_active_utterance(self):
        p = make_profile(AdvanceMode.TEXT, tts=True)
        self.ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        self.ctrl.observe(generation='generation', connection=1, sequence=1,
                          source=SOURCE, text='「こんにちは」', pid=10)
        original = self.ctrl.utterance
        p.advance_mode = AdvanceMode.AUDIO
        p.default_tts = False
        p.reading_ms = 3000
        p.per_reading_unit_wait_ms = 500
        self.assertEqual(self.ctrl.utterance, original)
        self.assertEqual(original.advance_mode, AdvanceMode.TEXT)
        self.assertEqual(original.text_wait_ms, 900)
        self.assertTrue(original.use_tts)

    def test_staged_profile_applies_only_at_next_utterance_boundary(self):
        p = make_profile(AdvanceMode.TEXT, tts=False)
        self.ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
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

    def test_staged_profile_cannot_change_active_capture_ownership(self):
        cases = ('hook', 'ocr')
        for source_kind in cases:
            with self.subTest(source_kind=source_kind):
                profile = make_profile()
                if source_kind == 'ocr':
                    profile.source = 'ocr'
                    profile.ocr = OCRSettings('tesseract', 'jpn', (0, 0, 640, 180), stable_samples=2)
                ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
                ctrl.begin(profile, Target(10, 20), 'ocr' if source_kind == 'ocr' else 'generation', 1)
                changed = deepcopy(profile)
                if source_kind == 'hook':
                    changed.hooks = [HookSelection('OTHER', 9, 10)]
                else:
                    changed.ocr = OCRSettings('tesseract', 'jpn', (0, 0, 800, 240), stable_samples=2)
                with self.assertRaises(ValueError):
                    ctrl.stage_profile(changed)

    def test_staged_profile_waits_through_same_utterance_duplicate(self):
        p = make_profile()
        self.ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
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

    def test_staged_repeat_policy_decides_next_identical_line(self):
        p = make_profile()
        self.ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        first = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                  source=SOURCE, text='「繰り返し」', pid=10)
        updated = make_profile()
        updated.id = p.id
        updated.hooks = list(p.hooks)
        updated.repeat_rule = 'each_flush'
        self.ctrl.stage_profile(updated)
        self.ctrl.permit(True)
        self.ctrl.presentation_event(first, 'text_elapsed')
        self.assertTrue(self.ctrl.try_advance())
        second = self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                   source=SOURCE, text='「繰り返し」', pid=10)
        self.assertIsNotNone(second)
        self.assertEqual(self.ctrl.profile.repeat_rule, 'each_flush')

    def test_unsent_new_hook_is_external_transition_without_disabling_auto(self):
        p = make_profile(AdvanceMode.AUDIO, tts=False)
        self.ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
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
        self.assertFalse(self.ctrl.native_acoustic_completed(first))

    def test_unsent_identical_hook_is_not_guessed_as_external_transition(self):
        p = make_profile(AdvanceMode.TEXT, tts=False)
        self.ctrl = Controller(lambda *_: None, lambda *_: True, TEST_G2P)
        self.ctrl.begin(p, Target(10, 20), 'generation', 1)
        first = self.ctrl.observe(generation='generation', connection=1, sequence=1,
                                  source=SOURCE, text='「同じ台詞」', pid=10, received_ns=1_000)
        self.ctrl.permit(True)
        self.assertIsNone(self.ctrl.observe(generation='generation', connection=1, sequence=2,
                                            source=SOURCE, text='「同じ台詞」', pid=10, received_ns=2_500))
        self.assertEqual(self.ctrl.token, first)
        self.assertTrue(self.ctrl.authorized)

class StorageModes(unittest.TestCase):
    def test_common_and_game_overrides_roundtrip(self):
        catalog = Catalog(common=CommonSettings(advance_mode=AdvanceMode.AUDIO,
                uncertain_text_fallback=True, default_tts=True, reading_ms=880,
                per_reading_unit_wait_ms=210))
        game = catalog.create_game('Example')
        stored = game.profiles[game.default_profile]
        stored.inherit_common = ['advance_mode', 'uncertain_text_fallback', 'per_reading_unit_wait_ms']
        stored.default_tts = False
        stored.reading_ms = 550
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings-v4.json'
            save(catalog, path)
            restored = load(path)
        p = restored.effective_profile(restored.games[game.id].profiles[stored.id])
        self.assertEqual(p.advance_mode, AdvanceMode.AUDIO)
        self.assertTrue(p.uncertain_text_fallback)
        self.assertEqual(p.per_reading_unit_wait_ms, 210)
        self.assertEqual(p.reading_ms, 550)
        self.assertFalse(p.default_tts)


if __name__ == '__main__':
    unittest.main()


class SharedPronunciationContract(unittest.TestCase):
    def test_explicit_ruby_is_shared_by_wait_and_audio_target(self):
        p = make_profile(AdvanceMode.AUDIO)
        seen = []
        class Backend:
            def candidates(self, text, *, max_candidates=4):
                seen.append((text, max_candidates))
                return (__import__('autoadv.g2p', fromlist=['PronunciationCandidate'])
                        .PronunciationCandidate(('m', 'i', 'y', 'a', 'k', 'o'), 0, 3),)
        utterance = interpret('「京(みやこ)」', p, session=1, line=1,
                              source='hook', sequence=1, g2p=Backend())
        self.assertEqual(utterance.text, '京(みやこ)')
        self.assertEqual(utterance.speech.resolved_text, 'みやこ')
        self.assertEqual(utterance.speech.alignment_text, 'みやこ')
        self.assertEqual(utterance.reading_unit_count, 3)
        self.assertEqual(utterance.speech.alignment_candidates[0].phonemes,
                         ('m', 'i', 'y', 'a', 'k', 'o'))
        self.assertEqual(seen, [('みやこ', 4)])
