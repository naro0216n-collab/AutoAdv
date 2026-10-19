from __future__ import annotations
import itertools
import unittest
from autoadv.contracts import (
    ContentKind, ControllerState as S, EffectKind as FX, Envelope, EventKind as E,
    Evidence, Playback, Presentation, ScreenState as SS, TTSPolicy as TP,
    VoicePresence as V,
)
from autoadv.controller import Controller, Settings
from autoadv.text import normalize


class Harness:
    def __init__(self, policy=TP.UNASSIGNED, voice=None, *, tts_required=False, calibrated=False):
        self.c = Controller(Settings(tts_required=tts_required,
                                     allow_heuristic_absence=calibrated), "session")
        self.now = 1000
        self.c.configure_tts(policy, selected_voice=voice, default_voice=voice)
        self.send(E.SCREEN_CHANGED, screen=SS.DIALOGUE)
        self.send(E.FOCUS_CHANGED, focused=True)
        self.send(E.CAPTURE_HEALTH, capture_ok=True)

    def send(self, kind, *, delta=1, **kwargs):
        self.now += delta
        line=self.c.line
        kwargs.setdefault("line_epoch", line.epoch if line else None)
        kwargs.setdefault("cancel_token", line.token if line else None)
        return self.c.accept(Envelope(kind, "session", self.now, **kwargs))

    def line(self, text="おはよう", *, ready=True, id="a", empty=False):
        effects = self.send(E.LINE_CANDIDATE, source_seq=self.c._last_source_seq+1,
                            source_line_id=id, text=text, text_ready=ready, empty_confirmed=empty)
        return effects

    def absent(self, evidence=Evidence.METADATA):
        return self.send(E.VOICE_PRESENCE, voice=V.ABSENT, evidence=evidence)

    def tick(self, delta=20000):
        return self.send(E.TIMER, delta=delta)

    def sent(self, effects):
        assert [x.kind for x in effects] == [FX.REQUEST_ADVANCE], effects
        x = effects[0]
        return self.c.confirm_advance(line_epoch=x.line_epoch, cancel_token=x.cancel_token,
                    fresh_screen=SS.DIALOGUE, fresh_focused=True,
                    fresh_text_ready=True, fresh_capture_ok=True,
                    fresh_native_voice_active=False, fresh_tts_playing=False,
                    fresh_line_matches=True, fresh_target_window_matches=True,
                    fresh_input_available=True, fresh_manual_override=False,
                    fresh_transition_pending=False, at_ms=self.now+1)


class ContractTests(unittest.TestCase):
    def test_silent_unassigned_once_ack(self):
        h=Harness();h.line();h.absent(); effects=h.tick()
        self.assertTrue(h.sent(effects));self.assertEqual(h.c.state,S.WAIT_TRANSITION_ACK)
        self.assertEqual(h.tick(),[])
        h.send(E.TRANSITION_ACK, transition_id="new-scene-1")
        self.assertEqual(h.c.state,S.WAIT_LINE)
        h.line("おはよう",id="b");h.absent();self.assertTrue(h.sent(h.tick()))

    def test_skip_and_missing_default_silent(self):
        for p in (TP.SKIP, TP.DEFAULT, TP.DISABLED, TP.UNASSIGNED):
            with self.subTest(p=p):
                h=Harness(p);h.line();h.absent();self.assertTrue(h.sent(h.tick()))
                self.assertEqual(h.c.line.presentation,Presentation.SILENT)

    def test_symbolic_and_phonetic(self):
        for s in ("……","――","！","…………"):
            self.assertEqual(normalize(s).kind,ContentKind.SYMBOLIC)
            h=Harness();h.line(s);h.absent();self.assertTrue(h.sent(h.tick()))
        for s in ("あっ","っ","ん", "京"):
            self.assertEqual(normalize(s).kind,ContentKind.SPEAKABLE)

    def test_empty_requires_confirmation(self):
        h=Harness();h.line("",empty=False)
        self.assertEqual(h.c.state,S.PAUSED)
        self.assertEqual(h.tick(),[])
        h=Harness();h.line("",empty=True);h.absent();self.assertTrue(h.sent(h.tick()))

    def test_does_not_invent_lines_on_blank(self):
        h=Harness();self.assertEqual(h.tick(),[]);self.assertEqual(h.c.state,S.WAIT_LINE)

    def test_partial_text_and_same_line_update(self):
        h=Harness();h.line("こん",ready=False)
        # Same source identity and updated candidate remains the SAME epoch.
        h.send(E.LINE_CANDIDATE, source_seq=2, source_line_id="a", text="こんにちは")
        self.assertEqual(h.c.line.epoch,1)
        self.assertEqual(h.tick(delta=1000),[])
        h.send(E.LINE_READY)
        h.absent();self.assertTrue(h.sent(h.tick()))

    def test_text_changes_after_ready_fail_closed(self):
        h=Harness();h.line("こんばんは")
        effect=h.send(E.LINE_CANDIDATE,source_seq=2,source_line_id="a",text="こんにちは")
        self.assertEqual(effect[0].kind,FX.STOPPED)
        self.assertEqual(h.c.state,S.PAUSED)

    def test_unattributed_line_change_pauses(self):
        h=Harness();h.line("a",id=None)
        h.send(E.LINE_CANDIDATE,source_seq=2,text="b")
        self.assertEqual(h.c.pause_reason,"unattributed_line_change")

    def test_unconfirmed_next_identical_stays_waiting(self):
        h=Harness();h.line();h.absent();self.assertTrue(h.sent(h.tick()))
        h.send(E.LINE_CANDIDATE,source_seq=2,source_line_id="a",text="おはよう")
        self.assertEqual(h.c.state,S.WAIT_TRANSITION_ACK)
        self.assertEqual(h.c.line.epoch,1)

    def test_new_line_identity_does_not_bypass_transition_ack(self):
        h=Harness();h.line();h.absent();self.assertTrue(h.sent(h.tick()))
        old=h.c.line
        h.send(E.LINE_CANDIDATE,source_seq=2,source_line_id="b",text="おはよう")
        self.assertIs(h.c.line,old)
        self.assertEqual(h.c.line.epoch,1)
        self.assertEqual(h.c.state,S.WAIT_TRANSITION_ACK)
        self.assertTrue(old.sent)
        h.send(E.TRANSITION_ACK,transition_id="visual-ready-cycle-b")
        self.assertEqual(h.c.state,S.WAIT_LINE)
        h.send(E.LINE_CANDIDATE,source_seq=3,source_line_id="b",text="おはよう")
        self.assertEqual(h.c.line.epoch,2)
        self.assertEqual(h.c.state,S.WAIT_TEXT_READY)

    def test_native_long_pause_and_completion(self):
        h=Harness();h.line("……")
        h.send(E.VOICE_PRESENCE,voice=V.PRESENT,evidence=Evidence.METADATA)
        self.assertEqual(h.tick(delta=1000),[])
        h.send(E.NATIVE_STARTED,native_utterance_id="v1",evidence=Evidence.AUDIO_CONFIRMED)
        self.assertEqual(h.tick(delta=1000),[])
        self.assertEqual(h.c.state,S.WAIT_NATIVE_END)
        self.assertEqual(h.send(E.NATIVE_COMPLETED,native_utterance_id="other",evidence=Evidence.AUDIO_CONFIRMED),[])
        self.assertEqual(h.tick(delta=1000),[])
        self.assertTrue(h.sent(h.send(E.NATIVE_COMPLETED,native_utterance_id="v1",evidence=Evidence.AUDIO_CONFIRMED)))

    def test_present_without_verified_onset_pauses(self):
        h=Harness();h.line();h.send(E.VOICE_PRESENCE,voice=V.PRESENT,evidence=Evidence.METADATA)
        effects=h.tick(delta=35000)
        self.assertEqual(effects[0].kind,FX.STOPPED)
        self.assertEqual(h.c.pause_reason,"native_start_or_end_unconfirmed")

    def test_native_active_without_verified_end_pauses(self):
        h=Harness();h.line();h.send(E.NATIVE_STARTED,
            native_utterance_id="v",evidence=Evidence.AUDIO_CONFIRMED)
        effects=h.tick(delta=180001)
        self.assertEqual(effects[0].kind,FX.STOPPED)
        self.assertEqual(h.c.pause_reason,"native_end_unconfirmed")

    def test_native_end_requires_attribution(self):
        h=Harness();h.line();h.absent()
        self.assertEqual(h.send(E.NATIVE_STARTED,evidence=Evidence.HEURISTIC,native_utterance_id="x"),[])
        self.assertEqual(h.c.line.voice,V.ABSENT)
        h.send(E.NATIVE_STARTED,evidence=Evidence.AUDIO_CONFIRMED,native_utterance_id="x")
        self.assertEqual(h.c.line.voice,V.PRESENT)
        self.assertEqual(h.send(E.NATIVE_COMPLETED,evidence=Evidence.AUDIO_CONFIRMED,native_utterance_id="y"),[])
        self.assertEqual(h.c.line.playback,Playback.ACTIVE)

    def test_capture_failure_pauses(self):
        h=Harness();h.line();h.send(E.CAPTURE_HEALTH,capture_ok=False)
        self.assertEqual(h.c.pause_reason,"audio_capture_unhealthy")
        self.assertEqual(h.tick(),[])

    def test_choices_stop(self):
        for state in (SS.CHOICE, SS.MAP, SS.MENU, SS.TRANSITION, SS.UNKNOWN):
            with self.subTest(state=state):
                h=Harness();h.line();h.absent();h.send(E.SCREEN_CHANGED,screen=state)
                self.assertEqual(h.c.state,S.PAUSED);self.assertEqual(h.tick(),[])

    def test_focus_lost_pauses(self):
        h=Harness();h.line();h.send(E.FOCUS_CHANGED,focused=False)
        self.assertEqual(h.c.state,S.PAUSED)

    def test_tts_emits_start_and_finish(self):
        h=Harness(TP.SELECTED,"speaker1");h.line();effects=h.absent()
        self.assertEqual(effects[0].kind,FX.START_TTS)
        self.assertEqual(effects[0].voice_id,"speaker1")
        self.assertEqual(h.tick(),[])
        self.assertTrue(h.sent(h.send(E.TTS_FINISHED,tts_success=True)))

    def test_tts_native_late_cancels_and_waits(self):
        h=Harness(TP.SELECTED,"speaker1");h.line();h.absent()
        effects=h.send(E.NATIVE_STARTED,native_utterance_id="native",evidence=Evidence.AUDIO_CONFIRMED)
        self.assertEqual([e.kind for e in effects],[FX.CANCEL_TTS])
        self.assertEqual(h.c.state,S.WAIT_NATIVE_END)
        self.assertEqual(h.send(E.TTS_FINISHED,tts_success=True),[])
        self.assertEqual(h.tick(),[])
        self.assertTrue(h.sent(h.send(E.NATIVE_COMPLETED,native_utterance_id="native",evidence=Evidence.AUDIO_CONFIRMED)))

    def test_late_metadata_cancels_tts_before_sound(self):
        h=Harness(TP.SELECTED,"s");h.line();h.absent()
        effects=h.send(E.VOICE_PRESENCE,voice=V.PRESENT,evidence=Evidence.METADATA)
        self.assertEqual(effects[0].kind,FX.CANCEL_TTS)
        self.assertEqual(h.tick(),[])

    def test_tts_error_silent_vs_required(self):
        h=Harness(TP.SELECTED,"s");h.line();h.absent();h.send(E.TTS_FINISHED,tts_success=False)
        self.assertTrue(h.sent(h.tick()))
        h=Harness(TP.SELECTED,"s",tts_required=True);h.line();h.absent();h.send(E.TTS_FINISHED,tts_success=False)
        self.assertEqual(h.c.state,S.PAUSED)

    def test_tts_voice_required_but_unassigned(self):
        h=Harness(TP.DEFAULT,tts_required=True);h.line();h.absent()
        self.assertEqual(h.c.state,S.PAUSED)

    def test_heuristic_voice_grace(self):
        h=Harness(calibrated=True);h.line("あ")
        h.absent(Evidence.HEURISTIC)
        self.assertEqual(h.c.line.presentation,Presentation.UNRESOLVED)
        self.assertEqual(h.tick(delta=300),[])
        self.assertTrue(h.sent(h.tick(delta=3000)))

    def test_uncalibrated_heuristic_fails_closed(self):
        h=Harness();h.line("あ");h.absent(Evidence.HEURISTIC)
        effects=h.tick(delta=100000)
        self.assertEqual(effects[0].kind,FX.STOPPED)
        self.assertEqual(h.c.pause_reason,"native_voice_unresolved")

    def test_unresolved_voice_never_times_out_to_absent(self):
        h=Harness();h.line();effects=h.tick(delta=100000)
        self.assertEqual([x.kind for x in effects],[FX.STOPPED])
        self.assertEqual(h.c.state,S.PAUSED)
        self.assertEqual(h.c.pause_reason,"native_voice_unresolved")

    def test_bgm_as_unattributed_audio_does_not_set_native(self):
        h=Harness();h.line();h.absent()
        h.send(E.NATIVE_STARTED,native_utterance_id=None,evidence=Evidence.NONE)
        self.assertEqual(h.c.line.voice,V.ABSENT)

    def test_preflight_blocks(self):
        h=Harness();h.line();h.absent();effect=h.tick()[0]
        self.assertFalse(h.c.confirm_advance(line_epoch=effect.line_epoch,
            cancel_token=effect.cancel_token,fresh_screen=SS.CHOICE,
            fresh_focused=True,fresh_text_ready=True,fresh_capture_ok=True,at_ms=h.now+1))
        self.assertFalse(h.c.line.sent)

    def test_manual_invalidation_before_advance(self):
        h=Harness();h.line();h.absent();effect=h.tick()[0]
        h.send(E.MANUAL_OVERRIDE)
        self.assertFalse(h.c.confirm_advance(line_epoch=effect.line_epoch,cancel_token=effect.cancel_token,
                        fresh_screen=SS.DIALOGUE,fresh_focused=True,fresh_text_ready=True,
                        fresh_capture_ok=True,at_ms=h.now+1))

    def test_delayed_stale_notifications(self):
        h=Harness(TP.SELECTED,"s");h.line();h.absent();old=h.c.line
        h.send(E.NATIVE_STARTED,native_utterance_id="n",evidence=Evidence.AUDIO_CONFIRMED)
        req=h.send(E.NATIVE_COMPLETED,native_utterance_id="n",evidence=Evidence.AUDIO_CONFIRMED)
        self.assertTrue(h.sent(req))
        h.send(E.TRANSITION_ACK,transition_id="ack")
        h.line("次",id="b")
        self.assertEqual(h.send(E.TTS_FINISHED,tts_success=True,line_epoch=old.epoch,cancel_token=old.token),[])
        self.assertEqual(h.c.line.epoch,2)

    def test_ack_timeout_no_resend(self):
        h=Harness();h.line();h.absent();self.assertTrue(h.sent(h.tick()))
        self.assertEqual(h.c.transition_timed_out(h.now+20000)[0].kind,FX.STOPPED)
        self.assertEqual(h.c.state,S.PAUSED)

    def test_bad_ack_identity(self):
        h=Harness();h.line();h.absent();self.assertTrue(h.sent(h.tick()))
        h.send(E.TRANSITION_ACK,transition_id=None)
        self.assertEqual(h.c.state,S.PAUSED)

    def test_long_text_no_cap(self):
        h=Harness();h.line("あ" * 1200);h.absent()
        self.assertEqual(h.tick(delta=3000),[])
        self.assertTrue(h.sent(h.tick(delta=120000)))

    def test_no_advance_when_tts_canceled_from_native(self):
        h=Harness(TP.SELECTED,"s");h.line();h.absent()
        h.send(E.NATIVE_STARTED,native_utterance_id="v",evidence=Evidence.AUDIO_CONFIRMED)
        h.send(E.TTS_FINISHED,tts_success=True)
        self.assertEqual(h.tick(),[])

    def test_strict_event_session_epoch_token(self):
        h=Harness();h.line()
        self.assertEqual(h.c.accept(Envelope(E.VOICE_PRESENCE,"wrong",h.now+1,
                line_epoch=1,cancel_token=1,voice=V.ABSENT,evidence=Evidence.METADATA)),[])
        self.assertEqual(h.c.accept(Envelope(E.VOICE_PRESENCE,"session",h.now+1,
                line_epoch=1,cancel_token=999,voice=V.ABSENT,evidence=Evidence.METADATA)),[])
        self.assertEqual(h.c.line.voice,V.UNRESOLVED)


if __name__ == "__main__": unittest.main()