"""Implementation-independent semantic properties of AutoAdv product domains."""
from __future__ import annotations

from types import SimpleNamespace
import numpy as np
import pytest

from autoadv.audio_buffer import PCMChunk
from autoadv.utterance_progress import (
    HumanVocalEvidence, LexicalProgressEvidence, NativeAudioObservation,
    VocalEvidenceSeries, VocalRecurrenceEvidence, VocalScoreSample, UtteranceProgressState,
    evaluate_utterance_progress,
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
    assert committed.observed_units == committed.target_units == 3
    assert committed.post_target_free_frames >= 7

    logits, vocab = logits_with_phones(('a', 'b', 'c'), frames=30, starts=(5, 10, 15))
    logits[23, 0] = 0.0
    logits[23, vocab['c']] = 7.0
    aligner = TargetConditionedAligner(Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c')))
    aligner.set_target('target')
    ambiguous = aligner.align(chunks(30))
    assert ambiguous.complete_alignment and not ambiguous.committed_target_end
    assert ambiguous.observed_units == 4


def _lexical_evidence(*, start=1100, aligned_end=1320, target_end=1320,
                      associated=True, complete=True, committed=True,
                      target_units=4, observed_units=None, aligned_units=4):
    return LexicalProgressEvidence(
        target_units=target_units,
        observed_units=target_units if observed_units is None else observed_units,
        aligned_target_units=aligned_units,
        target_associated=associated,
        complete_alignment=complete,
        committed_target_end=committed,
        target_start_ms=start,
        aligned_end_ms=aligned_end,
        target_end_ms=target_end,
        reading_rank=0,
        path_margin=3.0,
        boundary_margin=2.0,
    )


def _vocal_evidence(*series):
    return HumanVocalEvidence(series=tuple(
        VocalEvidenceSeries(
            observer=observer,
            active_score_threshold=.45,
            samples=tuple(VocalScoreSample(at, score, support) for at, score, support in samples),
        )
        for observer, samples in series
    ))


def _progress(*, lexical=None, vocal=None, end=2000, lower=1000, inputs=(), recurrence=None,
              recurrence_starts=None):
    def lookup(observer, start, _end):
        if recurrence_starts is not None:
            recurrence_starts.append((observer, start))
        if isinstance(recurrence, dict):
            item = recurrence.get(observer)
            return () if item is None else (item,)
        if recurrence is None:
            return ()
        if isinstance(recurrence, tuple):
            return recurrence
        return (recurrence,)
    result, measured_recurrence = evaluate_utterance_progress(
        capture_end_ms=end,
        lexical=lexical,
        vocal=vocal,
        causal_lower_bound_ms=lower,
        input_candidates_ms=tuple(inputs),
        recurrence_lookup=lookup if recurrence is not None else None,
    )
    return result, measured_recurrence


def test_alignment_selects_supported_pronunciation_candidate():
    logits, vocab = logits_with_phones(('a', 'd', 'c'), frames=30)
    aligner = TargetConditionedAligner(
        Posterior(logits, vocab), g2p=StaticG2P(('a', 'b', 'c'), ('a', 'd', 'c')))
    aligner.set_target('target')
    chosen = aligner.align(chunks(30))
    assert (chosen.reading_rank, chosen.target_phonemes, chosen.committed_target_end) == (
        1, ('a', 'd', 'c'), True)


def test_audio_progress_needs_measured_acoustic_relation_and_never_uses_elapsed_time_as_closure():
    open_target = _vocal_evidence(('speech', (
        (1120, .9, 1080), (1240, .9, 1200), (1360, .9, 1320),
    )))
    closed_target = _vocal_evidence(('speech', (
        (1120, .9, 1080), (1240, .9, 1200),
        (1500, .1, 1240), (1700, .1, 1500), (1900, .1, 1700),
    )))
    partial = _lexical_evidence(complete=False, committed=False, aligned_units=2)
    complete = _lexical_evidence()

    # A located finite-vocal risk stays protective until new measurements close
    # it; advancing only the evaluation frontier cannot manufacture an endpoint.
    for relation in (partial, complete):
        for frontier in (1400, 2400, 5200):
            progress, _ = _progress(lexical=relation, vocal=open_target, end=frontier)
            assert progress.state == UtteranceProgressState.ACTIVE

    partial_closed, _ = _progress(lexical=partial, vocal=closed_target, end=1900)
    complete_closed, _ = _progress(lexical=complete, vocal=closed_target, end=1900)
    assert partial_closed.state == UtteranceProgressState.UNKNOWN
    assert complete_closed.state == UtteranceProgressState.ENDED

    # A lexical end without an independently measured human-vocal relation is
    # not physical EOF evidence.
    for missing in (None, HumanVocalEvidence(), _vocal_evidence(('speech', (
        (1120, .1, 1080), (1400, .1, 1360), (1800, .1, 1760),
    )))):
        progress, _ = _progress(lexical=complete, vocal=missing, end=2000)
        assert progress.state == UtteranceProgressState.UNKNOWN


def test_audio_progress_uses_temporal_evidence_without_assigning_human_vocal_ownership():
    lexical = _lexical_evidence()
    speech = (
        (1120, .9, 1080), (1240, .9, 1200), (1320, .9, 1280),
        (1500, .1, 1320), (1800, .1, 1500),
    )
    continuing_tail = (
        (1400, .9, 1360), (1520, .9, 1480), (1640, .9, 1600),
    )
    closed_tail = continuing_tail + (
        (1800, .1, 1640), (2100, .1, 1800),
    )

    # Different observers need no cross-observer episode label. Any new open
    # finite-vocal activity after the Presentation anchor keeps cut risk active.
    active, _ = _progress(
        lexical=lexical,
        vocal=_vocal_evidence(('speech', speech), ('nonlexical', continuing_tail)),
        end=1800,
    )
    ended, _ = _progress(
        lexical=lexical,
        vocal=_vocal_evidence(('speech', speech), ('nonlexical', closed_tail)),
        end=2100,
    )
    assert active.state == UtteranceProgressState.ACTIVE
    assert ended.state == UtteranceProgressState.ENDED

    # Activity already present before the causal lower bound is not promoted to
    # current-Presentation risk merely because it overlaps the aligned text.
    preexisting = _vocal_evidence(('speech', (
        (900, .9, 860), (1120, .9, 1080), (1480, .9, 1440),
    )))
    progress, _ = _progress(lexical=lexical, vocal=preexisting, end=1600, lower=1000)
    assert progress.state == UtteranceProgressState.UNKNOWN

    # Quiet on another observer never closes this observer's open measurement.
    independent = _vocal_evidence(
        ('speech', ((1280, .9, 1260), (1480, .9, 1460))),
        ('nonlexical', ((1200, .1, 0), (1500, .1, 300), (1800, .1, 600))),
    )
    progress, _ = _progress(lexical=lexical, vocal=independent, end=1800)
    assert progress.state == UtteranceProgressState.ACTIVE


def test_vocal_risk_uses_observed_time_changes_and_current_recurrence():
    post_input_voice = _vocal_evidence(('nonlexical', (
        (1120, .9, 1080), (1240, .9, 1200), (1380, .9, 1340),
    )))
    no_relation, _ = _progress(vocal=post_input_voice, end=1400, lower=None)
    from_causal_fact, _ = _progress(vocal=post_input_voice, end=1400, lower=1000)
    from_external_candidate, _ = _progress(
        vocal=post_input_voice, end=1400, lower=None, inputs=(1000, 1250))
    only_late_candidates, _ = _progress(
        vocal=post_input_voice, end=1400, lower=None, inputs=(1200, 1300))
    assert no_relation.state == UtteranceProgressState.UNKNOWN
    assert from_causal_fact.state == UtteranceProgressState.ACTIVE
    assert from_external_candidate.state == UtteranceProgressState.ACTIVE
    assert only_late_candidates.state == UtteranceProgressState.UNKNOWN

    unchanged = _vocal_evidence(('nonlexical', (
        (960, .9, 0), (1240, .9, 280), (1480, .9, 520), (1720, .9, 760),
    )))
    changed = _vocal_evidence(('nonlexical', (
        (960, .9, 0), (1240, .1, 280), (1480, .1, 520), (1720, .9, 760),
    )))
    for lower, inputs in ((1000, ()), (None, (1000,))):
        stable_context, _ = _progress(
            vocal=unchanged, end=1720, lower=lower, inputs=inputs)
        same_run_with_valley, _ = _progress(
            vocal=changed, end=1720, lower=lower, inputs=inputs)
        assert stable_context.state == UtteranceProgressState.UNKNOWN
        # Presentation timing relates already-segmented physical activity. A
        # score valley too short to close that activity cannot be reinterpreted
        # as a second post-input onset by another state machine.
        assert same_run_with_valley.state == UtteranceProgressState.UNKNOWN

    stable = VocalRecurrenceEvidence(
        strength=.9, period_ms=500, prominence=.3)
    unstable = VocalRecurrenceEvidence(
        strength=.9, period_ms=500, prominence=.02)
    two_open = _vocal_evidence(
        ('speech', ((1120, .9, 1080), (1700, .9, 1660), (2300, .9, 2260))),
        ('nonlexical', ((1180, .9, 1140), (1800, .9, 1760), (2400, .9, 2360))),
    )
    recurrence_starts = []
    progress, _ = _progress(
        lexical=_lexical_evidence(complete=False, committed=False, observed_units=5),
        vocal=two_open, end=2500, lower=1000,
        recurrence={'speech': unstable, 'nonlexical': unstable},
        recurrence_starts=recurrence_starts,
    )
    assert progress.state == UtteranceProgressState.ACTIVE
    assert recurrence_starts == [('nonlexical', 1120), ('speech', 1120)]
    released, _ = _progress(
        lexical=_lexical_evidence(complete=False, committed=False, observed_units=5),
        vocal=two_open, end=2500, lower=1000,
        recurrence={'speech': stable, 'nonlexical': unstable},
    )
    assert released.state == UtteranceProgressState.UNKNOWN

    # Recurrence is an existence claim over measured period candidates. Generic
    # self-similarity at one lag cannot hide a weaker but period-specific lag.
    self_similar_not_periodic = VocalRecurrenceEvidence(
        strength=.95, period_ms=700, prominence=.01)
    periodic = VocalRecurrenceEvidence(
        strength=.8, period_ms=1800, prominence=.3)
    released, measured = _progress(
        lexical=_lexical_evidence(complete=False, committed=False, observed_units=5),
        vocal=two_open, end=2500, lower=1000,
        recurrence=(self_similar_not_periodic, periodic),
    )
    assert released.state == UtteranceProgressState.UNKNOWN
    assert measured == periodic

    # Reading amount is deliberately a rough gate. Before excess, a committed
    # textual endpoint keeps recurrence scoped to the post-target tail. Once
    # observed pronunciation exceeds the selected reading, the same already
    # measured recurrence may use the whole continuing activity instead.
    recurrence_starts = []
    before_excess, _ = _progress(
        lexical=_lexical_evidence(target_end=1320, observed_units=4),
        vocal=two_open, end=2500, lower=1000,
        recurrence={'speech': stable},
        recurrence_starts=recurrence_starts,
    )
    assert before_excess.state == UtteranceProgressState.UNKNOWN
    assert recurrence_starts == [('nonlexical', 1320), ('speech', 1320)]
    recurrence_starts.clear()
    after_excess, _ = _progress(
        lexical=_lexical_evidence(target_end=1320, observed_units=5),
        vocal=two_open, end=2500, lower=1000,
        recurrence={'speech': stable},
        recurrence_starts=recurrence_starts,
    )
    assert after_excess.state == UtteranceProgressState.UNKNOWN
    assert recurrence_starts == [('nonlexical', 1120), ('speech', 1120)]

    # Advancing only wall-clock evaluation time cannot close a measured open run.
    for frontier in (1400, 2400, 5200):
        progress, _ = _progress(vocal=post_input_voice, end=frontier, lower=1000)
        assert progress.state == UtteranceProgressState.ACTIVE


def test_unassociated_lexical_fragments_do_not_change_temporal_vocal_evidence():
    weak = _lexical_evidence(
        start=1400, aligned_end=1530, target_end=1530,
        associated=False, complete=False, committed=False, aligned_units=2)
    vocal = _vocal_evidence(('nonlexical', (
        (1120, .9, 1080), (1260, .1, 1120), (1450, .9, 1400), (1780, .9, 1740),
    )))

    timing_only, _ = _progress(vocal=vocal, end=1800, lower=1000)
    with_fragment, _ = _progress(lexical=weak, vocal=vocal, end=1800, lower=1000)
    unbounded, _ = _progress(lexical=weak, vocal=vocal, end=1800, lower=None)

    assert with_fragment == timing_only
    assert timing_only.state == UtteranceProgressState.ACTIVE
    assert unbounded.state == UtteranceProgressState.UNKNOWN


def test_short_score_valleys_do_not_become_endpoints_but_measured_quiet_can_close_a_run():
    samples = (
        (1120, .9, 1000),
        (1240, .1, 1120),
        (1360, .9, 1240),
        (1480, .1, 1360),
        (1600, .9, 1480),
    )
    for count in range(1, len(samples) + 1):
        progress, _ = _progress(
            vocal=_vocal_evidence(('nonlexical', samples[:count])),
            end=samples[count - 1][0], lower=1000)
        assert progress.state == UtteranceProgressState.ACTIVE

    closed = _vocal_evidence(('nonlexical', (
        (1120, .9, 1000),
        (1360, .1, 1120), (1660, .1, 1360),
    )))
    progress, _ = _progress(vocal=closed, end=1660, lower=1000)
    assert progress.state == UtteranceProgressState.UNKNOWN

    reopened = _vocal_evidence(('nonlexical', (
        (1120, .9, 1000),
        (1360, .1, 1120), (1660, .1, 1360),
        (1800, .9, 1760),
    )))
    progress, _ = _progress(vocal=reopened, end=1800, lower=1000)
    assert progress.state == UtteranceProgressState.ACTIVE


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

    pcm = chunks(frames=10)
    assert adapter(np.zeros((1, 4, 3), dtype=np.float32)).infer(pcm).shape == (4, 3)
    with pytest.raises(ValueError, match='unsupported logits shape'):
        adapter(np.zeros((2, 4, 3), dtype=np.float32)).infer(pcm)
