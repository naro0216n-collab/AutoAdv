"""Performance observations cannot alter the AutoAdv input or audio contracts."""
from __future__ import annotations
import json
from pathlib import Path
from threading import Event, Thread
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoadv.audio_buffer import PCMChunk
from autoadv.conversation import Token
from autoadv.native_audio import NativeAudio
from validation.real_game.trace import ValidationTrace
from autoadv.voice import VoiceJobs


class TimingG2P:
    def candidates(self, text, *, max_candidates=4):
        from autoadv.g2p import PronunciationCandidate
        return (PronunciationCandidate(('a', 'b', 'c')),) if max_candidates else ()


class TimingPosterior:
    frame_ms_hint = 20.0
    vocab = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3, 'a': 4, 'b': 5, 'c': 6}

    def infer(self, chunks):
        import numpy as np
        logits = np.full((30, len(self.vocab)), -4.0, dtype=np.float32)
        logits[:, 0] = 5.0
        for phone, frame in zip(('a', 'b', 'c'), (5, 10, 15)):
            logits[frame, 0] = 0.0
            logits[frame, self.vocab[phone]] = 7.0
        return logits


def _native_audio(*args, **kwargs):
    kwargs.setdefault('g2p', TimingG2P())
    kwargs.setdefault('model_backend', TimingPosterior())
    return NativeAudio(*args, **kwargs)


class PerformanceObservationTests(unittest.TestCase):
    def test_concurrent_trace_writers_keep_monotonic_sequence(self):
        with TemporaryDirectory() as root:
            trace = Path(root) / 'trace.jsonl'
            observer = ValidationTrace(str(trace))
            workers = [Thread(target=lambda: [observer.emit('measurement', ms=n)
                                               for n in range(20)]) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([entry['seq'] for entry in events[1:]], list(range(1, 81)))
            self.assertTrue(all(a['at_ns'] <= b['at_ns'] for a, b in zip(events, events[1:])))

    def test_alignment_worker_reports_latency_without_disclosing_transcript(self):
        import numpy as np
        token = Token(1, 2)
        raw = np.zeros((600 * 48, 2), dtype='<f4').tobytes()
        chunk = PCMChunk(600, raw, 48000, 2, 4)
        reports = []
        native = _native_audio(101, Path('unused'), Path('unused'), Path('unused'),
                               lambda *_: None,
                               on_inference=lambda ticket, measurements: reports.append((ticket, measurements)))
        try:
            native.history.start(101)
            native.begin(token, '猫はにゃーにゃーと鳴く。')
            native.history.push(chunk, target_pid=101)
            native._infer(token, native._generation, (chunk,), native.aligner)
            self.assertEqual(len(reports), 1)
            measured = reports[0][1]
            self.assertEqual(reports[0][0], token)
            self.assertEqual(measured['audio_ms'], 600)
            self.assertGreaterEqual(measured['align_ms'], 0)
            self.assertGreaterEqual(measured['queue_ms'], 0)
            self.assertEqual(measured['target_units'], 3)
            self.assertIn('path_margin', measured)
            self.assertNotIn('recognized', measured)
            self.assertNotIn('transcript', measured)
        finally:
            native.close()

    def test_tts_timing_does_not_replace_real_playback_confirmation(self):
        import io
        import wave
        with io.BytesIO() as stream:
            with wave.open(stream, 'wb') as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(24000)
                writer.writeframes(bytes(2400 * 2))
            wav = stream.getvalue()
        reported = []
        observed = []
        token = Token(9, 3)
        jobs = VoiceJobs(lambda *args: reported.append(args),
                         on_timing=lambda *args: observed.append(args))
        with patch('autoadv.voice.synthesize', return_value=wav), \
             patch('autoadv.voice.play', return_value=True):
            jobs.start(token, 'http://unused', 'こんにちは', 'voice')
            # The jobs are daemon threads; wait for their actual completion callback.
            from time import monotonic, sleep
            deadline = monotonic() + 2
            while not reported and monotonic() < deadline:
                sleep(.001)
        self.assertEqual(reported, [(token, True, '')])
        self.assertEqual([r[1] for r in observed], ['synthesis', 'playback'])
        self.assertEqual([r[3] for r in observed], [100, 100])
        self.assertTrue(all(r[2] >= 0 for r in observed))

    def test_latest_voice_job_supersedes_inflight_synthesis_without_stale_playback(self):
        import io
        import wave
        from time import monotonic, sleep

        with io.BytesIO() as stream:
            with wave.open(stream, 'wb') as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(24000)
                writer.writeframes(bytes(240 * 2))
            wav = stream.getvalue()

        first_started = Event()
        release_first = Event()
        synthesized = []
        played = []
        reported = []

        def fake_synthesize(_url, _reading, voice):
            synthesized.append(voice)
            if voice == 'voice-1':
                first_started.set()
                release_first.wait(2)
            return wav

        def fake_play(_audio, cancelled):
            played.append(cancelled.is_set())
            return not cancelled.is_set()

        jobs = VoiceJobs(lambda *args: reported.append(args))
        token = Token(4, 2)
        with patch('autoadv.voice.synthesize', side_effect=fake_synthesize), \
             patch('autoadv.voice.play', side_effect=fake_play):
            jobs.start(token, 'http://unused', '試聴', 'voice-1')
            self.assertTrue(first_started.wait(1))
            jobs.start(token, 'http://unused', '試聴', 'voice-2')
            deadline = monotonic() + 2
            while not reported and monotonic() < deadline:
                sleep(.001)
            release_first.set()
            deadline = monotonic() + 2
            while len(synthesized) < 2 and monotonic() < deadline:
                sleep(.001)
            sleep(.02)

        self.assertEqual(synthesized, ['voice-1', 'voice-2'])
        self.assertEqual(played, [False])
        self.assertEqual(reported, [(token, True, '')])



if __name__ == '__main__':
    unittest.main()
