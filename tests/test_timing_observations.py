"""Performance observations cannot alter the AutoAdv input or audio contracts."""
from __future__ import annotations
import json
from pathlib import Path
from threading import Thread
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoadv.audio_buffer import PCMChunk
from autoadv.conversation import Token
from autoadv.native_audio import NativeAudio
from autoadv.validation_trace import ValidationTrace
from autoadv.voice import VoiceJobs


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

    def test_real_worker_reports_decode_latency_without_disclosing_transcript(self):
        token = Token(1, 2)
        chunk = PCMChunk(100, bytes(960 * 8), 48000, 2, 4)
        reports = []
        class Recognizer:
            def recognize(self, chunks):
                self_test.assertEqual(chunks, (chunk,))
                return '猫はにゃーにゃーと鳴く。'
        class Activity:
            def observe_float32_stereo(self, frames):
                return type('Status', (), {'frames_analyzed': True,
                    'active': True, 'tail_silence_ms': 0})()
        self_test = self
        native = NativeAudio(101, Path('unused'), Path('unused'), Path('unused'),
                             lambda *_: None, recognizer=Recognizer(), activity=Activity(),
                             on_inference=lambda ticket, measurements: reports.append((ticket, measurements)))
        try:
            native.history.start(101)
            native.history.push(chunk, target_pid=101)
            native.begin(token, '猫はにゃーにゃーと鳴く。')
            native._infer(token, native._generation, (chunk,))
            self.assertEqual(len(reports), 1)
            measured = reports[0][1]
            self.assertEqual(reports[0][0], token)
            self.assertEqual(measured['audio_ms'], 20)
            self.assertGreaterEqual(measured['decode_ms'], 0)
            self.assertGreaterEqual(measured['queue_ms'], 0)
            self.assertTrue(measured['monotonic'])
            self.assertNotIn('recognized', measured)
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


if __name__ == '__main__':
    unittest.main()
