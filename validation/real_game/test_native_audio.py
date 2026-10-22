"""Real recorded Japanese audio through AutoAdv's native activity path.

This does NOT certify live LunaHook, Windows Process Loopback, GUI or targeted input.
No game build or launch is performed. The independent live suite handles those
AutoAdv responsibilities when an already running game is available.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import unittest
import wave
from types import SimpleNamespace

from autoadv.audio_buffer import PCMChunk
from autoadv.conversation import Token
from autoadv.native_audio import NativeAudio

GAME = Path(__file__).parent / "renpy" / "game"
MANIFEST = Path(__file__).parent / "VOICE_MANIFEST.json"


def replay_pcm_into_real_native_activity(file: Path, token: Token):
    import numpy as np
    events = []
    native = NativeAudio(
        42, Path("not-used-for-offline-samples.dll"),
        Path("unused-asr.onnx"), Path("unused-asr.tokens"),
        lambda t, name, reason: events.append((t, name, reason)), recognizer=SimpleNamespace(recognize=lambda _: ''),
        clock=lambda: 0,
    )
    try:
        native.begin(token, "台詞")
        with wave.open(str(file), "rb") as wav:
            assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
            assert wav.getframerate() == 48000
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        for frame in range(0, len(audio) - 959, 960):
            samples = audio[frame:frame + 960].astype("<f4") / 32768.0
            # Native process-loopback PCM ABI: float32 stereo at 48 kHz
            stereo = np.column_stack((samples, samples)).astype("<f4").tobytes()
            native.feed(PCMChunk(frame // 48, stereo, 48000, 2, 4))
        before_tail = tuple(events)
        start = (len(audio) // 960) * 20
        for ms in range(start, start + 1200, 20):
            native.feed(PCMChunk(ms, bytes(960 * 2 * 4), 48000, 2, 4))
        after_tail = tuple(events)
        native.cancel()
        return before_tail, after_tail, native.rise_ms, native.tail_ms
    finally:
        native.close()


class RealRecordedAudioContracts(unittest.TestCase):
    def test_real_recordings_provide_activity_but_vad_alone_never_completes(self):
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
                before, after, onset, tail_ms = replay_pcm_into_real_native_activity(file, ticket)
                self.assertGreaterEqual(onset, 0, 'recorded speech should be detected by VAD')
                self.assertGreaterEqual(tail_ms, 300)
                self.assertFalse(any(kind == 'completed' for _, kind, _ in after),
                                 'VAD without matching ASR must never complete a line')
        self.assertEqual(kinds, {"normal", "whisper"})

    def test_cancelled_utterance_cannot_emit_completion_from_old_recording(self):
        import numpy as np
        file = GAME / "audio" / "voice" / "normal_001.wav"
        with wave.open(str(file), "rb") as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        events = []
        observed_pcm = []
        native = NativeAudio(
            42, Path("not-used.dll"), Path("unused.onnx"), Path("unused.tokens"),
            lambda token, kind, _: events.append((token, kind)), recognizer=SimpleNamespace(recognize=lambda _: ''),
            on_pcm=lambda token, frames: observed_pcm.append((token, frames)),
            clock=lambda: 0,
        )
        old = Token(20, 5)
        try:
            native.begin(old, "えっ嘘でしょ。")
            for index in range(0, len(audio) - 959, 960):
                pcm = audio[index:index + 960].astype("<f4") / 32768.0
                frames = np.column_stack((pcm, pcm)).astype("<f4").tobytes()
                native.feed(PCMChunk(index // 48, frames, 48000, 2, 4))
            self.assertEqual(len(observed_pcm), 1)
            self.assertEqual(observed_pcm[0], (old, 960))
            native.cancel()
            for ms in range(2000, 3200, 20):
                native.feed(PCMChunk(ms, bytes(960 * 8), 48000, 2, 4))
            self.assertNotIn((old, "completed"), events)
        finally:
            native.close()


if __name__ == "__main__":
    unittest.main()
