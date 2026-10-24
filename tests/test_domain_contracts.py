"""Implementation-independent semantic properties of AutoAdv product domains."""
from __future__ import annotations

from types import SimpleNamespace
import numpy as np
import pytest

from autoadv.audio_buffer import PCMChunk
from autoadv.utterance_progress import HumanVocalEvidence, VocalRecurrenceEvidence
from autoadv.utterance_progress import (
    LexicalProgressEvidence, NativeAudioObservation, PresentationBoundaryCertainty,
    UtteranceProgressEstimator, UtteranceProgressState,
)
from autoadv.forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from autoadv.g2p import HaqumeiG2P, PronunciationCandidate, count_reading_units, reading_units_from_candidates
from autoadv.model import (ReplacementRule, SPEAKER_NEWLINE_BODY_RULE, StructureRule, interpret,
                          matching_structure_rule, resolve_speaker_enclosed_body)
from tests.contracts_support import profile
from tests.g2p_support import FakeG2P, TEST_G2P


class StaticG2P:
    def __init__(self, *readings):
        self.readings = readings

    def candidates(self, text, *, max_candidates=4):
        return tuple(PronunciationCandidate(tuple(reading), rank=i)
                     for i, reading in enumerate(self.readings[:max_candidates]))


class Posterior:
    frame_stride_ms = 20.0

    def __init__(self, logits, vocab):
        self.logits = np.asarray(logits, dtype=np.float32)
        self.vocab = vocab

    def infer(self, chunks):
        return self.logits


def chunks(frames=30):
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


def align(reading, heard, *, frames=30, starts=(5, 10, 15, 20, 24)):
    logits, vocab = logits_with_phones(heard, frames=frames, starts=starts)
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(reading))
    aligner.set_target('target')
    return aligner.align(chunks(frames))


def test_text_interpretation_keeps_display_content_separate_from_pronunciation_views():
    p = profile(tts=True)
    p.replacements = [
        ReplacementRule('こんにちは', 'はろー', target='tts'),
        ReplacementRule('東京', 'とうきょう', target='both'),
    ]
    g2p = FakeG2P(
        {'とうきょう': ('t', 'o', 'o', 'ky', 'o', 'o')},
    )
    u = interpret('大和「京さん東京こんにちは！」', p, session=1, line=1,
                  source='hook', sequence=1, g2p=g2p)
    assert (u.speaker, u.text) == ('大和', '京さん東京こんにちは！')
    assert u.speech.resolved_text == '京さんとうきょうこんにちは！'
    assert u.speech.tts_text == '京さんとうきょうはろー！'
    assert u.speech.alignment_text == '京さんとうきょうこんにちは！'

    for raw in (
        'これは説明です：https://example.org/voice',
        '彼は「京：おはよう」と書いた。',
        'また、東寺のように、\nはあ',
    ):
        prose = interpret(raw, profile(), session=1, line=2, source='hook', sequence=2, g2p=TEST_G2P)
        assert (prose.speaker, prose.text) == ('', raw)

    explicit_newline = profile()
    explicit_newline.source_rules['hook'] = [SPEAKER_NEWLINE_BODY_RULE]
    structured = interpret('明日香\n本文', explicit_newline, session=1, line=3,
                           source='hook', sequence=3, g2p=TEST_G2P)
    assert (structured.speaker, structured.text) == ('明日香', '本文')


def test_registered_readings_apply_only_to_complete_unprotected_word_spans():
    cases = (
        ('京(みやこ)', {'みやこ': '別読み'}, {'みやこ': ((0, 3),)}, 'みやこ'),
        ('東京の京', {'京': 'みやこ'}, {'東京の京': ((0, 2), (2, 3), (3, 4))}, '東京のみやこ'),
        ('大和京', {'大和京': 'やまとみやこ'}, {'大和京': ((0, 2), (2, 3))}, 'やまとみやこ'),
        ('甲乙', {'甲乙': 'こうおつ'}, {'甲乙': ((0, 1),)}, '甲乙'),
        ('ﾊﾟ京', {'京': 'みやこ'}, {'ﾊﾟ京': ((0, 1), (1, 2))}, 'ﾊﾟみやこ'),
    )
    for body, readings, spans, expected in cases:
        p = profile()
        p.name_readings.update(readings)
        u = interpret(f'「{body}」', p, session=1, line=1, source='x', sequence=1,
                      g2p=FakeG2P(word_spans=spans))
        assert u.speech.resolved_text == expected


def test_g2p_contract_preserves_primary_timing_and_fails_closed_on_unreadable_text():
    cases = (
        (('g', 'a', 'cl', 'k', 'o', 'o', 'N'), 5),
        (('ky', 'o', 'o'), 2),
        (('pau', 'sil', 'k'), 0),
        (('unk:固有名詞',), 4),
    )
    for phones, expected in cases:
        assert count_reading_units(phones) == expected

    primary = SimpleNamespace(words=[
        SimpleNamespace(phonemes=['k', 'a', 'r', 'e'], word='彼', mora_count=2),
        SimpleNamespace(phonemes=['pau'], word='。', mora_count=0),
    ])
    alternative = SimpleNamespace(words=[
        SimpleNamespace(phonemes=['a', 'n', 'o'], word='彼の', mora_count=3),
    ])
    engine = SimpleNamespace(g2p_candidates_detailed=lambda _: SimpleNamespace(
        candidates=[primary, alternative]))
    candidates = HaqumeiG2P(engine).candidates('彼の', max_candidates=2)
    assert candidates == (
        PronunciationCandidate(('k', 'a', 'r', 'e'), 0, 2),
        PronunciationCandidate(('a', 'n', 'o'), 1, 3),
    )
    assert reading_units_from_candidates('彼の', candidates[:1]) == 2
    p = profile()
    utterance = interpret('「彼の」', p, session=1, line=1, source='hook', sequence=1,
                          g2p=HaqumeiG2P(engine))
    assert utterance.reading_unit_count == 2
    assert utterance.text_wait_ms == max(p.reading_ms, 2 * p.per_reading_unit_wait_ms)
    with pytest.raises(ValueError, match='no primary pronunciation'):
        reading_units_from_candidates('東京', ())
    assert reading_units_from_candidates('……!?', ()) == 0


def test_speaker_enclosed_body_is_resolved_only_from_existing_speaker_identity():
    cases = (
        ('A（心の声）', 'A', (), ('A', '心の声')),
        ('Ａ（内心（補足））', 'A', (), ('A', '内心（補足）')),
        ('A「通常の台詞」', '', ('A',), ('A', '通常の台詞')),
        ('本文（補足）', 'A', (), ('A', '本文（補足）')),
        ('東京（とうきょう）', '', (), ('', '東京（とうきょう）')),
        ('未知「本文」', '', ('A',), ('', '未知「本文」')),
    )
    for body, speaker, configured, expected in cases:
        assert resolve_speaker_enclosed_body(body, speaker, configured) == expected


def test_g2p_word_spans_are_verified_in_canonical_coordinates_and_unknowns_remain_surface_specific():
    words = [
        SimpleNamespace(char_span=(0, 1), word='パ', phonemes=['p', 'a'], mora_count=1, is_unknown=False),
        SimpleNamespace(char_span=(1, 2), word='京', phonemes=['unk'], mora_count=0, is_unknown=True),
    ]
    candidate = SimpleNamespace(words=words)
    engine = SimpleNamespace(
        g2p_mapping_detailed=lambda text: words,
        g2p_candidates_detailed=lambda text: SimpleNamespace(candidates=[candidate]),
    )
    g2p = HaqumeiG2P(engine)
    assert [(span.start, span.end, span.surface) for span in g2p.word_spans('ﾊﾟ京')] == [
        (0, 1, 'パ'), (1, 2, '京')
    ]
    got = g2p.candidates('ﾊﾟ京')[0]
    assert got.phonemes == ('p', 'a', 'unk:京')
    assert got.reading_units == 2

    bad = SimpleNamespace(char_span=(0, 1), word='別', phonemes=['b'], mora_count=1, is_unknown=False)
    with pytest.raises(ValueError, match='cannot be projected'):
        HaqumeiG2P(SimpleNamespace(g2p_mapping_detailed=lambda text: [bad])).word_spans('京')


def test_forced_alignment_requires_target_evidence_and_right_context_from_one_path():
    absent = align(('a', 'b', 'c'), (), frames=30)
    prefix = align(('a', 'b', 'c', 'd', 'e'), ('a', 'b', 'c'), frames=30)
    edge = align(('a', 'b', 'c'), ('a', 'b', 'c'), frames=16, starts=(5, 10, 15))
    committed = align(('a', 'b', 'c'), ('a', 'b', 'c'), frames=30, starts=(5, 10, 15))

    assert (absent.target_associated, absent.complete_alignment, absent.committed_target_end) == (False, False, False)
    assert (prefix.target_associated, prefix.complete_alignment, prefix.committed_target_end) == (True, False, False)
    assert (edge.target_associated, edge.complete_alignment, edge.committed_target_end) == (True, True, False)
    assert (committed.target_associated, committed.complete_alignment, committed.committed_target_end) == (True, True, True)
    assert committed.post_target_free_frames >= 7

    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=30, starts=(5, 10, 15))
    logits[23, 0] = 0.0
    logits[23, vocab['c']] = 7.0
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    ambiguous = aligner.align(chunks(30))
    assert ambiguous.complete_alignment and not ambiguous.committed_target_end


def test_alignment_reading_selection_and_utterance_progress_preserve_evidence_meaning():
    logits, vocab = logits_with_phones(('a', 'd', 'c'), frames=30)
    aligner = TargetConditionedAligner(
        Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c'), ('a', 'd', 'c')))
    aligner.set_target('target')
    chosen = aligner.align(chunks(30))
    assert (chosen.reading_rank, chosen.target_phonemes, chosen.committed_target_end) == (1, ('a', 'd', 'c'), True)
    lexical = LexicalProgressEvidence.from_alignment(chosen, 1320)

    def vocal(*, first=1100, last=1580, end=1600, mean=.8, active=.8, pre=0.0,
              crosses_boundary=False, post_boundary_onset_ms=None):
        return HumanVocalEvidence(
            start_ms=1000, end_ms=end, frame_count=20, mean_probability=mean,
            peak_probability=.95, active_fraction=active, first_active_ms=first,
            last_active_ms=last, pre_epoch_active_fraction=pre,
            active_across_epoch_boundary=crosses_boundary,
            post_boundary_onset_ms=post_boundary_onset_ms)

    # Full forced alignment is lexical evidence, not an utterance endpoint.
    estimator = UtteranceProgressEstimator()
    active = estimator.observe(NativeAudioObservation(
        kind='acoustic', lexical=lexical, vocal=vocal(),
        recurrence=VocalRecurrenceEvidence(), capture_start_ms=1000, capture_end_ms=1600,
        boundary_at_ms=1000, boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert active.lexical_target_ended
    assert active.state == UtteranceProgressState.FINITE_VOCAL_ACTIVE

    # The same lexical completion becomes ENDED only after independent vocal-tail
    # evidence establishes that the finite vocal event is no longer active.
    ended = estimator.observe(NativeAudioObservation(
        kind='acoustic', lexical=lexical,
        vocal=vocal(last=1580, end=1800, mean=.1, active=.2),
        recurrence=VocalRecurrenceEvidence(), capture_start_ms=1000, capture_end_ms=1800,
        boundary_at_ms=1000, boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert ended.state == UtteranceProgressState.ENDED and ended.ended_at_ms == 1580

    # A recurring human source can continue after target lexical completion
    # without holding the finite target forever.
    looping = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', lexical=lexical, vocal=vocal(),
        recurrence=VocalRecurrenceEvidence(strength=.9, period_ms=900, period_stability=.8, recent_strength=.85),
        capture_start_ms=1000, capture_end_ms=1600, boundary_at_ms=1000,
        boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert looping.state == UtteranceProgressState.ENDED

    # A persistent source observed earlier must not erase a newly changed vocal
    # tail.  Without current recurrence evidence the finite tail remains active.
    changed_tail = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', lexical=lexical, vocal=vocal(),
        recurrence=VocalRecurrenceEvidence(
            strength=.9, period_ms=900, period_stability=.8, recent_strength=.2),
        capture_start_ms=1000, capture_end_ms=1600, boundary_at_ms=1000,
        boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert changed_tail.state == UtteranceProgressState.FINITE_VOCAL_ACTIVE

    # Cross-epoch human-vocal activity alone does not identify one persistent
    # source, and a target-associated but incomplete lexical path must remain a
    # finite vocal event even when recurrence also describes a background loop.
    partial = LexicalProgressEvidence(
        target_units=4, aligned_target_units=2, target_associated=True,
        complete_alignment=False, committed_target_end=False, target_end_ms=None,
        reading_rank=0, path_margin=3.0, boundary_margin=2.0)
    overlap = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', lexical=partial, vocal=vocal(pre=.9),
        recurrence=VocalRecurrenceEvidence(
            strength=.9, period_ms=900, period_stability=.8,
            cross_epoch_persistence=.9, recent_strength=.8),
        capture_start_ms=1000, capture_end_ms=1600, boundary_at_ms=1000,
        boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert overlap.state == UtteranceProgressState.FINITE_VOCAL_ACTIVE

    # Vocal activity that is already continuous across AutoAdv's presentation
    # boundary is not enough to establish a finite event for the new target.
    # A later measured onset can establish one without requiring lexical speech.
    continuity = UtteranceProgressEstimator()
    vad_continuity_only = continuity.observe(NativeAudioObservation(
        kind='acoustic', vocal=vocal(
            pre=.9, crosses_boundary=True, first=1010, last=1100,
            end=1400, mean=.2, active=.2),
        recurrence=VocalRecurrenceEvidence(), capture_start_ms=1000, capture_end_ms=1400,
        boundary_at_ms=1000, boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert vad_continuity_only.state == UtteranceProgressState.UNRESOLVED
    assert not vad_continuity_only.finite_vocal_observed
    assert not vad_continuity_only.persistent_vocal_observed

    new_nonlexical = continuity.observe(NativeAudioObservation(
        kind='acoustic', vocal=vocal(
            pre=.9, crosses_boundary=True, post_boundary_onset_ms=1500,
            first=1010, last=1580, end=1600),
        recurrence=VocalRecurrenceEvidence(), capture_start_ms=1000, capture_end_ms=1600,
        boundary_at_ms=1000, boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert new_nonlexical.state == UtteranceProgressState.FINITE_VOCAL_ACTIVE
    assert new_nonlexical.finite_vocal_observed

    # Lexical completion without any observed human vocal event does not invent
    # a finite-vocal state.  This keeps an actually unvoiced line eligible for
    # the Controller's explicit uncertainty fallback.
    unvoiced = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', lexical=lexical,
        vocal=vocal(first=None, last=None, end=1600, mean=0.0, active=0.0),
        recurrence=VocalRecurrenceEvidence(), capture_start_ms=1000, capture_end_ms=1600,
        boundary_at_ms=1000, boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    assert unvoiced.state == UtteranceProgressState.UNRESOLVED
    assert unvoiced.lexical_target_ended and not unvoiced.finite_vocal_observed

    # Non-lexical finite vocal events can finish under a strong AutoAdv boundary,
    # while the identical observations under an unknown external boundary do not
    # manufacture an endpoint from HOOK time.
    quiet = vocal(last=1250, end=1600, mean=.1, active=.2)
    auto = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', vocal=quiet, recurrence=VocalRecurrenceEvidence(),
        capture_start_ms=1000, capture_end_ms=1600, boundary_at_ms=1000,
        boundary_certainty=PresentationBoundaryCertainty.AUTOADV))
    external = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', vocal=quiet, recurrence=VocalRecurrenceEvidence(),
        capture_start_ms=1000, capture_end_ms=1600, boundary_at_ms=None,
        boundary_certainty=PresentationBoundaryCertainty.EXTERNAL_UNKNOWN))
    assert auto.state == UtteranceProgressState.ENDED
    assert external.state == UtteranceProgressState.FINITE_VOCAL_ENDING

    # The same distinction applies when delayed known text can be aligned
    # retrospectively.  Under an unknown external boundary, a complete lexical
    # match may describe old retained audio and therefore cannot manufacture an
    # audio-derived endpoint.
    external_lexical = UtteranceProgressEstimator().observe(NativeAudioObservation(
        kind='acoustic', lexical=lexical, vocal=quiet, recurrence=VocalRecurrenceEvidence(),
        capture_start_ms=1000, capture_end_ms=1600, boundary_at_ms=None,
        boundary_certainty=PresentationBoundaryCertainty.EXTERNAL_UNKNOWN))
    assert external_lexical.state == UtteranceProgressState.UNRESOLVED
    assert external_lexical.lexical_target_ended


class _FakeOnnxSession:
    def __init__(self, output):
        self.output = output

    def run(self, names, feeds):
        return [self.output]


def test_onnx_boundary_accepts_only_single_batch_posterior_shape():
    def adapter(output):
        model = object.__new__(OnnxPhoneCTCModel)
        model.vocab = {'PAD': 0, 'a': 1, 'b': 2}
        model.session = _FakeOnnxSession(np.asarray(output, dtype=np.float32))
        model.input_name = 'input_values'
        model.output_name = 'logits'
        return model

    assert adapter(np.zeros((1, 4, 3), dtype=np.float32)).infer(chunks(1)).shape == (4, 3)
    with pytest.raises(ValueError, match='unsupported logits shape'):
        adapter(np.zeros((2, 4, 3), dtype=np.float32)).infer(chunks(1))
