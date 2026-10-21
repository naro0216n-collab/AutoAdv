"""Product contracts for upstream Luna multi-HOOK routing and optional audio evidence."""
import tempfile
import unittest
from pathlib import Path
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


class VadOnlyContract(unittest.TestCase):
    def test_opt_in_vad_without_asr_waits_for_sustained_activity_and_silence(self):
        events = []
        decisions = iter([VADDecision(True, 0), VADDecision(True, 0),
                          VADDecision(False, 450)])
        class Activity:
            def observe_float32_stereo(self, data):
                return next(decisions)
        audio = NativeAudio(31, Path('not-installed.dll'), Path('missing.onnx'),
                            Path('missing.tokens'), lambda *v: events.append(v),
                            activity=Activity(), vad_only=True, clock=lambda: 0)
        try:
            token = Token(1, 1)
            audio.begin(token, '台詞')
            for ms in (100, 500, 1000):
                audio.feed(PCMChunk(ms, bytes(8), 48000, 2, 4))
            self.assertEqual([(e[0], e[1]) for e in events],
                             [(token, 'started'), (token, 'completed')])
            audio.cancel()
            self.assertIsNone(audio.token)
        finally:
            audio.close()

    def test_profile_owns_opt_in_and_wait_limit_across_save_reload(self):
        catalog = Catalog()
        g = catalog.create_game('game')
        p = g.profiles[g.default_profile]
        p.native_vad_only = True
        p.next_line_timeout_ms = 4200
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            save(catalog, path)
            loaded = load(path).games[g.id].profiles[p.id]
        self.assertTrue(loaded.native_vad_only)
        self.assertEqual(loaded.next_line_timeout_ms, 4200)
        loaded.next_line_timeout_ms = 10
        with self.assertRaises(ValueError):
            loaded.validate()


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Qt runtime is required')
class QtUpstreamHookConnection(unittest.TestCase):
    def test_selected_luna_threads_are_synchronized_and_unsynced_by_session(self):
        import os
        from unittest.mock import patch
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        catalog = Catalog()
        game = catalog.create_game('Two hook game')
        profile = game.profiles[game.default_profile]
        profile.hooks = [HookSelection('HT', 1, 2, 'text'), HookSelection('HS', 3, 4, 'speaker')]
        profile.speaker_links = {'HT|1|2': 'HS|3|4'}
        actions = []
        class FakeHookHost:
            def command(self, *args, **kwargs):
                actions.append((args, kwargs))
            def close(self):
                pass
        def run_checks(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.pid.setValue(31)
                window.hook_host = FakeHookHost()
                window.host_generation = 'host'
                window.connection_generation = 2
                window.controller.begin(profile, Target(31, 90), 'host', 2)
                window.sources = {TEXT.identity: TEXT, SPEAKER.identity: SPEAKER}
                window.sync_hook_threads()
                self.assertEqual([x[0] for x in actions],
                                 [('sync_thread',), ('sync_thread',)])
                window._output_group.extend([msg(TEXT, 1, '「はい」'), msg(SPEAKER, 2, '京')])
                window.present = lambda token: None  # no device needed for this routing contract
                window.flush_selected_outputs()
                self.assertEqual(window.controller.utterance.speaker, '京')
                window.stop_session()
                self.assertEqual([x[1]['enabled'] for x in actions],
                                 [True, True, False, False])
                self.assertEqual(window._synced_sources, {})
            finally:
                window.close()
            return 0
        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', run_checks):
                self.assertEqual(run_gui(), 0)
