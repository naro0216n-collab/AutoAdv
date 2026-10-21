"""Product contracts for upstream Luna multi-HOOK routing and optional audio evidence."""
import tempfile
import unittest
from pathlib import Path
import json
from autoadv.conversation import Controller, Target, Token
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import Profile, HookSelection, AdvanceMode
from autoadv.storage import Catalog, save, load
from autoadv.native_audio import NativeAudio
from autoadv.audio_buffer import PCMChunk
from autoadv.audio_match import VADDecision


TEXT = ThreadSource(31, 0x100, 1, 2, 'HT', 'text')
SPEAKER = ThreadSource(31, 0x200, 3, 4, 'HS', 'speaker')

def msg(source, seq, text):
    return HookEvent('output', 'host', 2, seq, seq * 1_000_000, 31, source, text)


class MultiHookProductContract(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.p = Profile('p', 'test', hooks=[HookSelection('HT', 1, 2, 'text'),
                                              HookSelection('HS', 3, 4, 'speaker')],
                         speaker_links={'HT|1|2': 'HS|3|4'},
                         default_tts=True, default_voice='default',
                         speaker_tts={'京': False})
        self.c = Controller(lambda *_: self.sent.append(1), lambda _: True)
        self.c.begin(self.p, Target(31, 90), 'host', 2)

    def test_speaker_after_text_still_routes_one_utterance(self):
        token = self.c.observe_group([msg(TEXT, 1, '「はい」'), msg(SPEAKER, 2, '京')])
        self.assertEqual(token, Token(self.c.session, 1))
        self.assertEqual(self.c.utterance.speaker, '京')
        self.assertEqual(self.c.utterance.use_tts, False)
        self.assertEqual(self.c.utterance.raw, '「はい」')
        self.assertEqual(self.c.highest_sequence, 2)

    def test_speaker_before_text_still_routes_one_utterance(self):
        token = self.c.observe_group([msg(SPEAKER, 1, '京'), msg(TEXT, 2, '「はい」')])
        self.assertIsNotNone(token)
        self.assertEqual(self.c.utterance.speaker, '京')

    def test_unrelated_speakers_do_not_guess_pairing(self):
        self.p.hooks.append(HookSelection('UNRELATED', 5, 6, 'speaker'))
        self.c.begin(self.p, Target(31, 90), 'host', 2)
        other = ThreadSource(31, 0x300, 5, 6, 'UNRELATED', 'other')
        token = self.c.observe_group([msg(TEXT, 1, '台詞'), msg(other, 2, '別人')])
        self.assertIsNotNone(token)
        self.assertEqual(self.c.utterance.speaker, '')

    def test_ambiguous_multiple_texts_fail_closed(self):
        self.c.permit(True)
        self.assertIsNone(self.c.observe_group([msg(TEXT, 1, '台詞A'), msg(TEXT, 2, '台詞B')]))
        self.assertEqual(self.c.reason, 'ambiguous_multihook_lines')
        self.assertFalse(self.c.authorized)
        self.assertEqual(self.sent, [])

    def test_no_next_output_after_input_returns_control_without_retry(self):
        self.c.permit(True)
        ticket = self.c.observe_group([msg(TEXT, 1, '台詞')])
        self.c.presentation_event(ticket, 'text_elapsed')
        self.c.presentation_event(ticket, 'tts_finished')
        self.assertTrue(self.c.try_advance())
        self.assertFalse(self.c.expire_next_line(Token(self.c.session, 2)))
        self.assertTrue(self.c.expire_next_line(ticket))
        self.assertEqual(self.c.reason, 'next_line_timeout')
        self.assertFalse(self.c.authorized)
        self.assertFalse(self.c.try_advance())
        self.assertEqual(self.sent, [1])
        # A late dialogue line is displayed, without silently restoring input rights.
        late = self.c.observe_group([msg(TEXT, 2, '次の台詞')])
        self.assertIsNotNone(late)
        self.assertFalse(self.c.authorized)

    def test_timeout_of_old_ticket_cannot_pause_new_line(self):
        self.c.permit(True)
        first = self.c.observe_group([msg(TEXT, 1, '一')])
        self.c.presentation_event(first, 'text_elapsed')
        self.c.try_advance()
        next_token = self.c.observe_group([msg(TEXT, 2, '二')])
        self.assertNotEqual(first, next_token)
        self.assertFalse(self.c.expire_next_line(first))
        self.assertTrue(self.c.authorized)


class NativeRecognitionContract(unittest.TestCase):
    def test_asr_model_and_tokens_are_required_without_injected_recognizer(self):
        with self.assertRaises(FileNotFoundError):
            NativeAudio(31, Path('unused.dll'), Path('missing.onnx'),
                        Path('missing.tokens'), lambda *_: None)

    def test_vad_activity_and_silence_cannot_complete_without_matching_asr(self):
        events = []
        decisions = iter([VADDecision(True, 0), VADDecision(True, 0),
                          VADDecision(False, 450)])

        class Activity:
            def observe_float32_stereo(self, _frames):
                return next(decisions)

        class NoMatchRecognizer:
            def recognize(self, _samples):
                return ''

        audio = NativeAudio(31, Path('unused.dll'), Path('missing.onnx'),
                            Path('missing.tokens'), lambda _t, name, _d: events.append(name),
                            activity=Activity(), recognizer=NoMatchRecognizer(), clock=lambda: 0)
        try:
            audio.begin(Token(1, 1), '台詞')
            for ms in (100, 500, 1000):
                audio.feed(PCMChunk(ms, bytes(8), 48000, 2, 4))
            self.assertFalse(audio.attributed)
            self.assertFalse(audio.completed)
            self.assertNotIn('completed', events)
            self.assertNotIn('started', events)
        finally:
            audio.close()

    def test_profile_has_two_modes_and_no_vad_only_setting(self):
        catalog = Catalog()
        g = catalog.create_game('game')
        p = g.profiles[g.default_profile]
        p.advance_mode = AdvanceMode.AUDIO
        p.next_line_timeout_ms = 4200
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            save(catalog, path)
            raw = json.loads(path.read_text(encoding='utf-8'))
            self.assertNotIn('native_vad_only', raw['games'][g.id]['profiles'][p.id])
            # Obsolete persisted options must never resurrect a second
            # native-completion policy; keep all unrelated profile data.
            raw['games'][g.id]['profiles'][p.id]['native_vad_only'] = True
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
            loaded_catalog = load(path)
            loaded = loaded_catalog.games[g.id].profiles[p.id]
            save(loaded_catalog, path)
            self.assertNotIn('native_vad_only', path.read_text(encoding='utf-8'))
        self.assertEqual(loaded.advance_mode, AdvanceMode.AUDIO)
        self.assertEqual(loaded.next_line_timeout_ms, 4200)
        self.assertFalse(hasattr(loaded, 'native_vad_only'))
        loaded.next_line_timeout_ms = 10
        with self.assertRaises(ValueError):
            loaded.validate()


if __name__ == '__main__':
    unittest.main()
