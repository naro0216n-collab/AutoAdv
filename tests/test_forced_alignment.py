from __future__ import annotations

import numpy as np

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_knowledge import NativeAudioObservation, NativeVoiceKnowledge, reduce_native_voice_knowledge
from autoadv.forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from autoadv.g2p import PronunciationCandidate


class StaticG2P:
    def __init__(self, *readings):
        self.readings = readings

    def candidates(self, text, *, max_candidates=4):
        return tuple(PronunciationCandidate(tuple(reading), rank=i)
                     for i, reading in enumerate(self.readings[:max_candidates]))


class Posterior:
    frame_ms_hint = 20.0

    def __init__(self, logits, vocab):
        self.logits = np.asarray(logits, dtype=np.float32)
        self.vocab = vocab

    def infer(self, chunks):
        return self.logits


def chunks(frames=30):
    # 20 ms per synthetic model frame -> 600 ms total for 30 frames.
    sample_count_48k = frames * 20 * 48
    raw = np.zeros((sample_count_48k, 2), dtype='<f4').tobytes()
    return (PCMChunk(at_ms=1000 + frames * 20, frames=raw,
                     sample_rate=48000, channels=2, sample_width_bytes=4),)


def logits_with_phones(sequence, *, frames=30, starts=(5, 10, 15, 20, 24)):
    vocab = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3,
             'a': 4, 'b': 5, 'c': 6, 'd': 7, 'e': 8}
    logits = np.full((frames, len(vocab)), -4.0, dtype=np.float32)
    logits[:, 0] = 5.0
    for phone, frame in zip(sequence, starts):
        logits[frame, 0] = 0.0
        logits[frame, vocab[phone]] = 7.0
    return logits, vocab


def test_target_absent_cannot_become_associated_or_complete():
    logits, vocab = logits_with_phones((), frames=30)
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    result = aligner.align(chunks(30))
    assert not result.target_associated
    assert not result.complete_alignment
    assert not result.committed_end
    assert result.rejection == 'insufficient_model_support'


def test_supported_prefix_associates_without_claiming_end():
    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=30)
    aligner = TargetConditionedAligner(
        Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c', 'd', 'e')))
    aligner.set_target('target')
    result = aligner.align(chunks(30))
    assert result.target_associated
    assert result.aligned_target_units == 3
    assert not result.complete_alignment
    assert not result.committed_end


def test_complete_target_at_capture_edge_does_not_commit_end():
    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=16, starts=(5, 10, 15))
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    result = aligner.align(chunks(16))
    assert result.target_associated
    assert result.complete_alignment
    assert result.target_end_frame == 16
    assert result.post_target_free_frames == 0
    assert not result.committed_end


def test_complete_target_commits_only_after_same_path_has_right_context():
    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=30, starts=(5, 10, 15))
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    result = aligner.align(chunks(30))
    assert result.target_associated
    assert result.complete_alignment
    assert result.committed_end
    assert result.target_end_frame == 16
    assert result.post_target_free_frames >= 7
    # Free spans are path ownership only; the type carries no silence/BGM label.
    assert result.unaligned_spans


def test_later_terminal_phone_evidence_keeps_earlier_boundary_uncommitted():
    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=30, starts=(5, 10, 15))
    # A later equally strong final-phone event makes the terminal boundary move;
    # it must not preserve/commit the earlier candidate end.
    logits[23, 0] = 0.0
    logits[23, vocab['c']] = 7.0
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    result = aligner.align(chunks(30))
    assert result.complete_alignment
    assert result.target_end_frame == 16
    assert result.fit.boundary_margin == 0.0
    assert not result.committed_end  # later equally good completion leaves the boundary ambiguous


def test_reading_candidate_can_win_only_on_model_evidence():
    logits, vocab = logits_with_phones(('a', 'd', 'c'), frames=30)
    aligner = TargetConditionedAligner(
        Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c'), ('a', 'd', 'c')))
    aligner.set_target('target')
    result = aligner.align(chunks(30))
    assert result.reading_rank == 1
    assert result.target_phonemes == ('a', 'd', 'c')
    assert result.committed_end


def test_knowledge_accepts_only_committed_alignment_end_and_is_monotonic():
    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=30)
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    complete = aligner.align(chunks(30))
    observation = NativeAudioObservation(
        kind='alignment', alignment=complete, capture_start_ms=1000,
        capture_end_ms=1600, target_acoustic_end_ms=1320)
    knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observation)
    assert knowledge.target_associated
    assert knowledge.target_end_confirmed
    assert knowledge.end.estimated_end_ms == 1320

    absent_logits, _ = logits_with_phones((), frames=30)
    later = TargetConditionedAligner(Posterior(absent_logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    later.set_target('target')
    regressed = NativeAudioObservation(kind='alignment', alignment=later.align(chunks(30)))
    preserved = reduce_native_voice_knowledge(knowledge, regressed)
    assert preserved == knowledge


def test_transport_observation_never_manufactures_association_or_end():
    observation = NativeAudioObservation(kind='transport', pcm_last_frame_ms=1200,
                                         heartbeat_at_ms=1250)
    knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observation)
    assert not knowledge.target_associated
    assert not knowledge.target_end_confirmed


class _FakeOnnxSession:
    def __init__(self, output):
        self.output = output

    def run(self, names, feeds):
        return [self.output]


def _onnx_adapter_with_output(output, *, vocab=None):
    model = object.__new__(OnnxPhoneCTCModel)
    model.vocab = vocab or {'PAD': 0, 'a': 1, 'b': 2}
    model.session = _FakeOnnxSession(np.asarray(output, dtype=np.float32))
    model.input_name = 'input_values'
    model.output_name = 'logits'
    return model


def test_onnx_adapter_normalizes_single_batch_logits_to_posterior_contract():
    adapter = _onnx_adapter_with_output(np.zeros((1, 4, 3), dtype=np.float32))
    result = adapter.infer(chunks(1))
    assert result.shape == (4, 3)


def test_onnx_adapter_rejects_logits_outside_posterior_contract():
    adapter = _onnx_adapter_with_output(np.zeros((2, 4, 3), dtype=np.float32))
    try:
        adapter.infer(chunks(1))
    except ValueError as exc:
        assert 'unsupported logits shape' in str(exc)
    else:
        raise AssertionError('batched logits must be rejected by the model adapter')
