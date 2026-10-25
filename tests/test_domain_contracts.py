"""Implementation-independent semantic properties of AutoAdv product domains."""
from __future__ import annotations

from types import SimpleNamespace
import numpy as np
import pytest

from autoadv.audio_buffer import PCMChunk
from autoadv.utterance_progress import (
    HumanVocalEvidence, LexicalProgressEvidence, NativeAudioObservation,
    VocalEvidenceSeries, VocalRecurrenceEvidence, UtteranceProgressState,
    evaluate_audio_snapshot,
)
from autoadv.forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from autoadv.g2p import HaqumeiG2P, PronunciationCandidate, count_reading_units, reading_units_from_candidates
from autoadv.model import (ReplacementRule, SpeakerStyle, SPEAKER_NEWLINE_BODY_RULE, StructureRule,
                          build_speaker_caption, interpret, matching_structure_rule,
                          resolve_speaker_enclosed_body)
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


def test_speaker_caption_contains_only_explicit_voice_design_dimensions():
    cases = (
        (SpeakerStyle(), ''),
        (SpeakerStyle(timbre='soft'), '柔らかい声。'),
        (SpeakerStyle(pace='slightly_slow'), 'ややゆっくり話す。'),
        (SpeakerStyle(age='young', gender='feminine', pitch='slightly_high', timbre='soft',
                      demeanor='calm', delivery='composed', pace='slightly_slow',
                      intonation='natural'),
         '若い女性の、やや高めで柔らかい声。穏やかな雰囲気で、落ち着いて、ややゆっくり、自然な抑揚で話す。'),
    )
    for style, expected in cases:
        assert build_speaker_caption(style) == expected


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


def test_alignment_reading_selection_and_audio_judgment_preserve_evidence_roles():
    logits, vocab = logits_with_phones(('a', 'd', 'c'), frames=30)
    aligner = TargetConditionedAligner(
        Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c'), ('a', 'd', 'c')))
    aligner.set_target('target')
    chosen = aligner.align(chunks(30))
    assert (chosen.reading_rank, chosen.target_phonemes, chosen.committed_target_end) == (
        1, ('a', 'd', 'c'), True)

    def lexical(*, start=1100, aligned_end=1320, target_end=1320, complete=True,
                committed=True, associated=True):
        return LexicalProgressEvidence.from_alignment(
            chosen, target_start_ms=start, aligned_end_ms=aligned_end,
            target_end_ms=target_end) if (complete and committed and associated) else LexicalProgressEvidence(
                target_units=4, aligned_target_units=2, target_associated=associated,
                complete_alignment=complete, committed_target_end=committed,
                target_start_ms=start, aligned_end_ms=aligned_end, target_end_ms=target_end,
                reading_rank=0, path_margin=3.0, boundary_margin=2.0)

    def vocal(*, intervals=(), start=900, end=2000, source='speech'):
        return HumanVocalEvidence(
            start_ms=start, end_ms=end,
            series=(VocalEvidenceSeries(
                source=source, frame_count=max(1, len(intervals) * 4),
                active_count=len(intervals) * 4,
                peak_probability=.9 if intervals else 0.0,
                active_intervals=tuple(intervals)),))

    def observation(*, lex=None, voc=None, rec=None, end=1600, lower=1000,
                    attached=1050):
        return NativeAudioObservation(
            kind='snapshot', lexical=lex, vocal=voc,
            recurrence=rec or VocalRecurrenceEvidence(),
            capture_start_ms=900, capture_end_ms=end,
            causal_lower_bound_ms=lower, target_attached_ms=attached)

    # A known-text prefix is direct evidence that a finite target utterance is
    # still in progress. A committed text end is not enough while connected
    # human-vocal support is still physically continuing.
    prefix = evaluate_audio_snapshot(observation(
        lex=lexical(complete=False, committed=False),
        voc=vocal(intervals=((1100, 1580),)), end=1600))
    assert prefix.state == UtteranceProgressState.ACTIVE

    continuing_snapshot = observation(
        lex=lexical(), voc=vocal(intervals=((1100, 1580), (1700, 1980)), end=2000),
        end=2000)
    continuing = evaluate_audio_snapshot(continuing_snapshot)
    assert continuing.state == UtteranceProgressState.ACTIVE
    # The reducer has no inference history: identical evidence is identical
    # regardless of how many previous evaluations happened.
    assert evaluate_audio_snapshot(continuing_snapshot) == continuing

    ended = evaluate_audio_snapshot(observation(
        lex=lexical(), voc=vocal(intervals=((1100, 1580),), end=2100), end=2100))
    assert ended.state == UtteranceProgressState.ENDED
    assert ended.ended_at_ms == 1580

    stable_loop = VocalRecurrenceEvidence(
        strength=.9, period_ms=600, period_stability=.85,
        recent_strength=.86, level_consistency=.9,
        low_vocal_reference_similarity=.2)
    loop_ended = evaluate_audio_snapshot(observation(
        lex=lexical(), voc=vocal(intervals=((1100, 2980),), end=3000),
        rec=stable_loop, end=3000))
    assert loop_ended.state == UtteranceProgressState.ENDED

    # Recurrence remains distinct evidence: a similar periodic structure already
    # present where no vocal observer is positive cannot, by itself, terminate
    # the finite target continuation.
    explained_loop = VocalRecurrenceEvidence(
        strength=.9, period_ms=600, period_stability=.85,
        recent_strength=.86, level_consistency=.9,
        low_vocal_reference_similarity=.88)
    still_active = evaluate_audio_snapshot(observation(
        lex=lexical(), voc=vocal(intervals=((1100, 2980),), end=3000),
        rec=explained_loop, end=3000))
    assert still_active.state == UtteranceProgressState.ACTIVE

    # Lack of an observer measurement is not observed quiet.  A committed
    # lexical end cannot become a physical endpoint merely because the vocal
    # measurement series is absent.
    unmeasured = evaluate_audio_snapshot(observation(
        lex=lexical(), voc=HumanVocalEvidence(start_ms=900, end_ms=2100), end=2100))
    assert unmeasured.state == UtteranceProgressState.UNKNOWN

    # Non-lexical human-vocal change after an observed input lower bound is a
    # finite-vocal cut risk, not source ownership. It suppresses fallback while
    # active, then returns to UNKNOWN when quiet or stable recurrence removes
    # that risk; it never manufactures ENDED.
    nonlex_active = evaluate_audio_snapshot(observation(
        voc=vocal(intervals=((1120, 1380),), end=1400), end=1400))
    assert nonlex_active.state == UtteranceProgressState.ACTIVE

    nonlex_quiet = evaluate_audio_snapshot(observation(
        voc=vocal(intervals=((1120, 1380),), end=1800), end=1800))
    assert nonlex_quiet.state == UtteranceProgressState.UNKNOWN

    nonlex_loop = evaluate_audio_snapshot(observation(
        voc=vocal(intervals=((1120, 2980),), end=3000), rec=stable_loop, end=3000))
    assert nonlex_loop.state == UtteranceProgressState.UNKNOWN

    # Positive activity already present at the causal lower bound is not
    # relabelled as old/background; it simply cannot establish that a new finite
    # vocal risk began after the input. A separated later change can.
    crossing_only = evaluate_audio_snapshot(observation(
        voc=vocal(intervals=((900, 1390),), end=1400), end=1400))
    assert crossing_only.state == UtteranceProgressState.UNKNOWN
    crossing_then_new = evaluate_audio_snapshot(observation(
        voc=vocal(intervals=((900, 1240), (1700, 1980)), end=2000), end=2000))
    assert crossing_then_new.state == UtteranceProgressState.ACTIVE

    # HOOK arrival is knowledge timing only. Changing it while every acoustic
    # measurement and causal bound remains identical cannot change the answer.
    early_hook = observation(
        voc=vocal(intervals=((1120, 1380),), end=1400), end=1400, attached=1200)
    late_hook = observation(
        voc=vocal(intervals=((1120, 1380),), end=1400), end=1400, attached=5200)
    assert evaluate_audio_snapshot(early_hook) == evaluate_audio_snapshot(late_hook)

    # Without an observed lower bound or lexical relation, non-lexical activity
    # remains uncertain rather than gaining an invented presentation identity.
    unbounded = evaluate_audio_snapshot(observation(
        voc=vocal(intervals=((1120, 1380),), end=1400), end=1400, lower=None))
    assert unbounded.state == UtteranceProgressState.UNKNOWN


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
