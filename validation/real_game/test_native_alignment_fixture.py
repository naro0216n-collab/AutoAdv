"""Recorded-audio fixture integrity and NativeAudio plumbing.

Real model quality is measured by benchmark_alignment and the Windows accuracy
path.  These tests only prove that committed recordings enter the production PCM
preprocessing/lifecycle without a VAD or transcription side route.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import unittest
import wave

import numpy as np

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_knowledge import NativeVoiceKnowledge, reduce_native_voice_knowledge
from autoadv.conversation import Token
from autoadv.forced_alignment import OnnxPhoneCTCModel
from autoadv.g2p import PronunciationCandidate
from autoadv.native_audio import NativeAudio

HERE = Path(__file__).parent
GAME = HERE / 'renpy' / 'game'
MANIFEST = HERE / 'VOICE_MANIFEST.json'


class StaticG2P:
    def candidates(self, text, *, max_candidates=4):
        return (PronunciationCandidate(('a', 'b', 'c')),)


class BlankPosterior:
    frame_ms_hint = 20.0
    vocab = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3, 'a': 4, 'b': 5, 'c': 6}

    def __init__(self):
        self.calls = 0
        self.input_frames = 0

    def infer(self, chunks):
        self.calls += 1
        self.input_frames += sum(len(chunk.frames) // 8 for chunk in chunks)
        logits = np.full((30, len(self.vocab)), -4.0, dtype=np.float32)
        logits[:, 0] = 6.0
        return logits


def read_mono(file: Path) -> np.ndarray:
    with wave.open(str(file), 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (48000, 1, 2):
            raise ValueError(file)
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')


def chunk_from_mono(mono: np.ndarray, end_ms: int) -> PCMChunk:
    pcm = mono.astype('<f4') / 32768.0
    stereo = np.column_stack((pcm, pcm)).astype('<f4').tobytes()
    return PCMChunk(end_ms, stereo, 48000, 2, 4)


def wait_idle(native: NativeAudio) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with native._lock:
            if not native._inflight:
                return
        time.sleep(.002)
    raise TimeoutError('native alignment did not settle')


class RecordedAudioFixtureContracts(unittest.TestCase):
    def test_manifest_recordings_are_integral_and_preprocess_to_16khz(self):
        manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['samples']), 14)
        self.assertEqual({sample['speaker'] for sample in manifest['samples']}, {'normal', 'whisper'})
        for sample in manifest['samples']:
            with self.subTest(sample=sample['id']):
                file = GAME / sample['path']
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), sample['sha256'])
                mono = read_mono(file)
                chunk = chunk_from_mono(mono, round(len(mono) / 48))
                prepared = OnnxPhoneCTCModel._waveform((chunk,))
                self.assertTrue(np.isfinite(prepared).all())
                self.assertLessEqual(abs(len(prepared) - round(len(mono) / 3)), 1)

    def test_real_recording_reaches_alignment_backend_without_vad_and_blank_model_cannot_end(self):
        sample = json.loads(MANIFEST.read_text(encoding='utf-8'))['samples'][0]
        mono = read_mono(GAME / sample['path'])
        model = BlankPosterior()
        observations = []
        errors = []
        native = NativeAudio(
            42, Path('unused-helper.dll'), Path('unused.onnx'), Path('unused.json'),
            lambda token, reason: errors.append((token, reason)),
            model_backend=model, g2p=StaticG2P(), clock=lambda: 1000,
            on_observation=lambda token, item: observations.append((token, item)),
        )
        token = Token(777, 1)
        try:
            native.history.start(42)
            native.boundary(1000)
            native.begin(token, sample['text'])
            chunk = chunk_from_mono(mono, 1000 + round(len(mono) / 48))
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
            wait_idle(native)
            self.assertFalse(errors)
            self.assertEqual(model.calls, 1)
            self.assertGreater(model.input_frames, 0)
            self.assertEqual(observations[0][0], token)
            knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observations[0][1])
            self.assertFalse(knowledge.target_associated)
            self.assertFalse(knowledge.target_end_confirmed)
            self.assertNotIn('vad_positive_packets', native.capture_status())
        finally:
            native.close()

    def test_cancelled_utterance_cannot_emit_from_subsequent_pcm(self):
        model = BlankPosterior()
        observations = []
        native = NativeAudio(
            42, Path('unused-helper.dll'), Path('unused.onnx'), Path('unused.json'),
            lambda *_: None, model_backend=model, g2p=StaticG2P(), clock=lambda: 1000,
            on_observation=lambda token, item: observations.append((token, item)),
        )
        try:
            native.history.start(42)
            native.begin(Token(20, 5), 'target')
            native.cancel()
            native.feed(PCMChunk(2000, bytes(960 * 8), 48000, 2, 4))
            time.sleep(.01)
            self.assertFalse(observations)
            self.assertEqual(model.calls, 0)
        finally:
            native.close()


if __name__ == '__main__':
    unittest.main()
