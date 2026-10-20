"""Stale callback and manual intervention ordering, without game-state scanners."""
import unittest
from autoadv.contracts import EventKind as E, Envelope, Evidence, VoicePresence, ControllerState as S
from autoadv.controller import Controller, Settings

class ConcurrentEventContracts(unittest.TestCase):
    def setUp(self):
        self.c=Controller(Settings(autoplay=True),session_id='s')
        self.send(E.FOCUS_CHANGED,1,focused=True)
        self.send(E.CAPTURE_HEALTH,2,capture_ok=True)
        self.send(E.LINE_CANDIDATE,3,source_seq=1,text='本文',source_kind='hook',
                  source_origin='selected',source_generation=1,text_ready=True)
    def send(self,kind,at,**fields):
        l=self.c.line
        return self.c.accept(Envelope(kind,'s',at,line_epoch=l.epoch if l else None,
                 cancel_token=l.token if l else None,**fields))
    def test_manual_override_latches_against_late_audio(self):
        self.send(E.MANUAL_OVERRIDE,102)
        self.send(E.NATIVE_STARTED,104,native_utterance_id='voice',evidence=Evidence.AUDIO_CONFIRMED)
        self.send(E.TIMER,105)
        self.assertEqual(self.c.state,S.WAIT_LINE)
        self.assertIsNone(self.c.line)
        self.assertFalse(self.c.autoplay_authorized)
    def test_cross_session_audio_cannot_affect_line(self):
        l=self.c.line
        self.c.accept(Envelope(E.VOICE_PRESENCE,'other',200,line_epoch=l.epoch,
            cancel_token=l.token,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA))
        self.assertEqual(self.c.line.voice,VoicePresence.UNRESOLVED)
    def test_stale_native_callback_does_not_update_new_epoch(self):
        l=self.c.line
        self.send(E.VOICE_PRESENCE,4,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA)
        request=self.send(E.TIMER,3000)[0]
        self.assertTrue(self.c.confirm_advance(line_epoch=request.line_epoch,
            cancel_token=request.cancel_token,fresh_focused=True,fresh_capture_ok=True,
            fresh_native_voice_active=False,fresh_tts_playing=False,
            fresh_target_window_matches=True,fresh_input_available=True,
            fresh_manual_override=False,at_ms=3001))
        self.send(E.LINE_CANDIDATE,3002,source_seq=2,text='新しい本文',source_kind='hook',
            source_origin='selected',source_generation=1)
        self.c.accept(Envelope(E.NATIVE_STARTED,'s',3003,line_epoch=l.epoch,
            cancel_token=l.token,native_utterance_id='old',evidence=Evidence.AUDIO_CONFIRMED))
        self.assertEqual(self.c.line.voice,VoicePresence.UNRESOLVED)
