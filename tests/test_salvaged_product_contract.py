"""Only product contracts, not legacy implementation promises."""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import copy
import unittest

from autoadv.model import (Profile, AdvanceMode, StructureRule, ReplacementRule,
                           OCRSettings, interpret, DEFAULT_STRUCTURE_RULES)
from autoadv.storage import Catalog, save, load
from autoadv.import_luna import inspect_luna, add_luna_games
from autoadv.conversation import Controller, Target
from autoadv.audio_match import TargetMatcher, phonetic_key, VADDecision
from autoadv.audio_buffer import PCMChunk
from autoadv.native_audio import NativeAudio


class SalvagedTextTests(unittest.TestCase):
    def test_quoted_newline_and_custom_priority(self):
        p = Profile('p', 'P')
        p.source_rules['src'] = [StructureRule(r'^#(?P<speaker>[^:]+):(?P<body>.*)$'), *DEFAULT_STRUCTURE_RULES]
        a = interpret('#春:おはよう', p, session=1, line=1, source='src', sequence=1)
        self.assertEqual((a.speaker, a.text), ('春', 'おはよう'))
        b = interpret('冬\nおやすみ', p, session=1, line=2, source='src', sequence=2)
        self.assertEqual((b.speaker, b.text), ('冬', 'おやすみ'))
        self.assertEqual(a.source, 'src')

    def test_ruby_names_and_independent_asr_tts_rules(self):
        p = Profile('p', 'P')
        p.name_readings = {'京': 'みやこ'}
        p.replacements = [ReplacementRule('A', 'えー', target='tts'),
                          ReplacementRule('B', 'びー', target='asr'),
                          ReplacementRule('C+', 'しー', regex=True, target='both')]
        u = interpret('京「京(みやこ)と東京とABCCC」', p, session=1, line=1, source='s', sequence=1)
        self.assertEqual(u.reading, 'みやこと東京とえーBしー')
        self.assertEqual(u.match_text, '京と東京とAびーしー')

    def test_explicit_speaker_tts_wins_over_default_native(self):
        p = Profile('p', 'P', default_tts=False)
        p.speaker_tts['大和'] = True
        p.speaker_voices['大和'] = 'voice-01'
        u = interpret('大和「こんにちは」', p, session=2, line=5, source='X', sequence=6)
        self.assertEqual((u.use_tts, u.voice), (True, 'voice-01'))

    def test_invalid_regexes_fail_before_persisting(self):
        for constructor in (lambda: StructureRule(r'(?P<other>.*)'),
                            lambda: ReplacementRule('(', '', regex=True)):
            with self.assertRaises(ValueError): constructor()


class SalvagedProfileTests(unittest.TestCase):
    def test_profile_crud_does_not_mutate_original_copy(self):
        c = Catalog()
        game = c.create_game('Game')
        p = game.profiles[game.default_profile]
        p.name_readings['京'] = 'みやこ'
        duplicate = c.duplicate_profile(game.id, p.id, 'Alternate')
        duplicate.name_readings['京'] = 'きょう'
        self.assertEqual(p.name_readings['京'], 'みやこ')
        c.rename_profile(game.id, duplicate.id, 'New name')
        game.default_profile = duplicate.id
        c.delete_profile(game.id, duplicate.id)
        self.assertEqual(game.default_profile, p.id)
        with self.assertRaises(ValueError): c.delete_profile(game.id, p.id)

    def test_ocr_and_structures_persist_without_volatile_identifiers(self):
        c = Catalog()
        game = c.create_game('Game')
        p = game.profiles[game.default_profile]
        p.source = 'ocr'
        p.ocr = OCRSettings('tesseract', 'jpn', (50, 60, 700, 130))
        p.inserted_hooks = ['HS7']
        p.source_rules['ocr'] = [StructureRule(r'(?P<body>.*)')]
        p.replacements = [ReplacementRule(r'\d+', 'number', True)]
        p.native_helper = 'C:/loopback.dll'
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            save(c, path)
            restored = load(path)
            q = restored.games[game.id].profiles[p.id]
            self.assertEqual(q.ocr, p.ocr)
            self.assertEqual(q.source_rules, p.source_rules)
            self.assertEqual(q.replacements, p.replacements)
            self.assertEqual(q.inserted_hooks, p.inserted_hooks)
            self.assertNotIn('host_generation', path.read_text())

    def test_luna_import_requires_confirmation_to_save_and_is_idempotent_by_uid(self):
        original = [['legacy'], {'legacy': {'gamepath': 'C:/Game.exe',
                          'hook': [[777, 890, 1, 2, 'thread', 'HS7']],
                          'needinserthookcode': ['HS8']}}]
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'luna.json'
            path.write_text(json.dumps(original), encoding='utf-8')
            games = inspect_luna(path)
            c = Catalog()
            candidate = add_luna_games(c, games)
            self.assertFalse(c.games)  # Import produces preview candidate, never changes original catalog.
            game = next(iter(candidate.games.values()))
            profile = game.profiles[game.default_profile]
            self.assertEqual(profile.hooks[0].code, 'HS7')
            self.assertEqual(profile.inserted_hooks, ['HS8'])
            self.assertEqual(json.loads(path.read_text()), original)
            with self.assertRaisesRegex(ValueError, 'already imported'):
                add_luna_games(candidate, games)

    def test_unknown_legacy_format_is_not_implicitly_mapped(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'x.json'
            path.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError): inspect_luna(path)


class AcquisitionAndInputTests(unittest.TestCase):
    def test_ocr_session_rejects_stale_and_duplicate_messages(self):
        p = Profile('p', 'OCR', source='ocr', reading_ms=10)
        sent = []
        c = Controller(lambda target, key: sent.append(key), lambda target: True)
        session = c.begin(p, Target(11, 22), 'ocr', 1)
        a = c.observe_ocr(sequence=1, text='同じ台詞', pid=11, session=session)
        self.assertIsNotNone(a)
        c.permit(True)
        c.presentation_event(a, 'text_elapsed')
        self.assertTrue(c.try_advance())
        self.assertIsNone(c.observe_ocr(sequence=2, text='同じ台詞', pid=11, session=session))
        self.assertIsNone(c.observe_ocr(sequence=3, text='', pid=11, session=session))
        b = c.observe_ocr(sequence=4, text='同じ台詞', pid=11, session=session)
        self.assertNotEqual(a, b)  # Observed blank between repeated OCR lines.
        self.assertIsNone(c.observe_ocr(sequence=5, text='stale', pid=11, session=session-1))
        self.assertEqual(sent, [13])

    def test_hook_only_validation_at_monitoring_not_when_editing_profile(self):
        p = Profile('p', 'Edit first')
        p.validate()
        with self.assertRaisesRegex(ValueError, 'body source'):
            Controller(lambda *_: None, lambda _: True).begin(p, Target(11, 22), 'h', 1)

    def test_target_matcher_does_not_complete_unrelated_or_unstable_voice(self):
        m = TargetMatcher('おはようございます')
        self.assertLess(m.observe('明日').coverage, .9)
        m2 = TargetMatcher('おはよう')
        m2.observe('おは')
        a = m2.observe('おはよう')
        b = m2.observe('おはよう')
        self.assertGreaterEqual(b.coverage, .9)
        self.assertEqual(b.tail_confidence, 1.0)


class NativeAudioSessionTests(unittest.TestCase):
    def test_autoadv_boundary_can_be_applied_before_hook_binding(self):
        class Recognizer:
            def recognize(self, chunks):
                return ''
        class VAD:
            def reset(self):
                pass
            def observe_float32_stereo(self, frames):
                return VADDecision(False, 0)
        audio = NativeAudio(11, Path('not-installed.dll'), Path('not-installed.onnx'), Path('no.tokens'),
                            lambda *_: None, recognizer=Recognizer(), activity=VAD(), clock=lambda: 3000)
        from autoadv.conversation import Token
        try:
            audio.history.start(11)
            for ts in (1200, 1800, 2400):
                audio.history.push(PCMChunk(ts, b'\x00' * 80, 48000, 2, 4), target_pid=11)
            audio.boundary(1500)
            token = Token(1, 1)
            audio.begin(token, '次の台詞')
            self.assertEqual(audio.window_start_ms, 1500)
            self.assertEqual([c.at_ms for c in audio.history.since(audio.window_start_ms)], [1800, 2400])
        finally:
            audio.close()

    def test_callback_on_attributed_matching_voice_only(self):
        class Recognizer:
            def recognize(self, chunks):
                return 'おはよう'
        class VAD:
            def __init__(self): self.index = 0
            def observe_float32_stereo(self, frames):
                self.index += 1
                return VADDecision(self.index == 1, 300 if self.index > 1 else 0)
        events = []
        audio = NativeAudio(11, Path('not-installed.dll'), Path('not-installed.onnx'), Path('no.tokens'),
                 lambda token, event, reason: events.append((token, event)),
                 recognizer=Recognizer(), activity=VAD(), clock=lambda: 1000)
        from autoadv.conversation import Token
        token = Token(1, 2)
        try:
            audio.history.start(11)
            audio.begin(token, 'おはよう')
            # The helper callback stores PCM before sending it to the monitor.
            for ts in (1000, 1600, 2200):
                chunk = PCMChunk(ts, b'\x00'*80, 48000, 2, 4)
                audio.history.push(chunk, target_pid=11)
                audio.feed(chunk)
                import time
                for _ in range(100):
                    with audio._lock:
                        busy = audio._inflight
                    if not busy: break
                    time.sleep(.001)
            self.assertIn((token, 'started'), events)
            self.assertIn((token, 'completed'), events)
            audio.cancel()
            self.assertIsNone(audio.token)
        finally:
            audio.close()


if __name__ == '__main__':
    unittest.main()

class StoredSettingsTransitionTests(unittest.TestCase):
    def test_initial_v3_replacements_are_migrated_once_into_one_rule_engine(self):
        c = Catalog()
        game = c.create_game('Existing')
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            save(c, path)
            data = json.loads(path.read_text(encoding='utf-8'))
            data['games'][game.id]['profiles'][game.default_profile]['replacements'] = {'京': 'みやこ'}
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            restored = load(path)
            p = restored.games[game.id].profiles[game.default_profile]
            self.assertEqual(p.replacements, [ReplacementRule('京', 'みやこ')])
            save(restored, path)
            after = json.loads(path.read_text(encoding='utf-8'))
            self.assertIsInstance(after['games'][game.id]['profiles'][p.id]['replacements'], list)
