"""Real recorded Japanese audio through AutoAdv's native observation path.

This does NOT certify live LunaHook, Windows Process Loopback, GUI or targeted input.
No game build or launch is performed. The independent live suite handles those
AutoAdv responsibilities when an already running game is available.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time
import unittest
import wave
from types import SimpleNamespace

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_knowledge import NativeVoiceKnowledge, reduce_native_voice_knowledge
from autoadv.conversation import Token
from autoadv.native_audio import NativeAudio

GAME = Path(__file__).parent / "renpy" / "game"
MANIFEST = Path(__file__).parent / "VOICE_MANIFEST.json"


class IdentityG2P:
    def candidates(self, text, *, max_candidates=4):
        from autoadv.audio_match import surface_key
        from autoadv.g2p import PronunciationCandidate
        units = tuple(surface_key(text))
        return (PronunciationCandidate(units),) if units and max_candidates else ()


TEST_G2P = IdentityG2P()

def _native_audio(*args, **kwargs):
    kwargs.setdefault('g2p', TEST_G2P)
    return NativeAudio(*args, **kwargs)


def _wait_for_inference(native: NativeAudio) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with native._lock:
            busy = native._inflight
        if not busy:
            return
        time.sleep(.002)
    raise TimeoutError('native observation did not settle')


def replay_pcm_into_real_native_activity(file: Path, token: Token):
    import numpy as np
    errors = []
    evidence = []
    native = _native_audio(
        42, Path("not-used-for-offline-samples.dll"),
        Path("unused-asr.onnx"), Path("unused-asr.tokens"),
        lambda t, reason: errors.append((t, reason)),
        recognizer=SimpleNamespace(recognize=lambda _: ''),
        on_observation=lambda t, item: evidence.append((t, item)),
        clock=lambda: 0,
    )
    try:
        native.history.start(42)
        native.begin(token, "台詞")
        with wave.open(str(file), "rb") as wav:
            assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
            assert wav.getframerate() == 48000
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        for frame in range(0, len(audio) - 959, 960):
            samples = audio[frame:frame + 960].astype("<f4") / 32768.0
            stereo = np.column_stack((samples, samples)).astype("<f4").tobytes()
            chunk = PCMChunk(frame // 48, stereo, 48000, 2, 4)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
        start = (len(audio) // 960) * 20
        for ms in range(start, start + 1200, 20):
            chunk = PCMChunk(ms, bytes(960 * 2 * 4), 48000, 2, 4)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
        _wait_for_inference(native)
        return tuple(errors), tuple(evidence), native.vad_onset_ms, native.vad_tail_ms
    finally:
        native.close()


class RealRecordedAudioContracts(unittest.TestCase):
    def test_real_recordings_produce_vad_observations_without_fabricating_target_end(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["samples"]), 14)
        kinds = set()
        for sample in manifest["samples"]:
            with self.subTest(audio=sample["id"]):
                file = GAME / sample["path"]
                self.assertTrue(file.is_file())
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), sample["sha256"])
                kinds.add(sample["speaker"])
                ticket = Token(777, 1)
                errors, evidence, onset, vad_tail_ms = replay_pcm_into_real_native_activity(file, ticket)
                self.assertFalse(errors)
                self.assertGreaterEqual(onset, 0, 'recorded speech should be detected by VAD')
                self.assertGreaterEqual(vad_tail_ms, 300)
                self.assertTrue(evidence)
                self.assertTrue(all(t == ticket for t, _ in evidence))
                knowledge = NativeVoiceKnowledge()
                for _, item in evidence:
                    knowledge = reduce_native_voice_knowledge(knowledge, item)
                self.assertFalse(
                    knowledge.target_end_confirmed,
                    'VAD plus an empty ASR alignment must not fabricate target completion')
        self.assertEqual(kinds, {"normal", "whisper"})

    def test_cancelled_utterance_cannot_emit_new_evidence_for_old_recording(self):
        import numpy as np
        file = GAME / "audio" / "voice" / "normal_001.wav"
        with wave.open(str(file), "rb") as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        errors = []
        evidence = []
        observed_pcm = []
        native = _native_audio(
            42, Path("not-used.dll"), Path("unused.onnx"), Path("unused.tokens"),
            lambda token, reason: errors.append((token, reason)),
            recognizer=SimpleNamespace(recognize=lambda _: ''),
            on_pcm=lambda token, frames: observed_pcm.append((token, frames)),
            on_observation=lambda token, item: evidence.append((token, item)),
            clock=lambda: 0,
        )
        old = Token(20, 5)
        try:
            native.history.start(42)
            native.begin(old, "えっ嘘でしょ。")
            for index in range(0, len(audio) - 959, 960):
                pcm = audio[index:index + 960].astype("<f4") / 32768.0
                frames = np.column_stack((pcm, pcm)).astype("<f4").tobytes()
                chunk = PCMChunk(index // 48, frames, 48000, 2, 4)
                native.history.push(chunk, target_pid=42)
                native.feed(chunk)
            self.assertEqual(len(observed_pcm), 1)
            self.assertEqual(observed_pcm[0], (old, 960))
            _wait_for_inference(native)
            before_cancel = len(evidence)
            native.cancel()
            for ms in range(2000, 3200, 20):
                native.feed(PCMChunk(ms, bytes(960 * 8), 48000, 2, 4))
            time.sleep(.01)
            self.assertEqual(len(evidence), before_cancel)
            self.assertFalse(errors)
        finally:
            native.close()


if __name__ == "__main__":
    unittest.main()
