"""Validation-only serialization of product semantic and telemetry events."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock
import time


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class ValidationTrace:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._sequence = 0
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps({
                'event': 'trace_started', 'version': 2, 'at_ns': time.monotonic_ns()
            }, ensure_ascii=False) + '\n')

    def emit(self, event: str, **fields: object) -> None:
        with self._lock:
            self._sequence += 1
            item = {'event': event, 'seq': self._sequence, 'at_ns': time.monotonic_ns(), **fields}
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')


class ProductTraceObserver:
    """Translate generic product events into the validation JSONL contract."""

    def __init__(self, trace: ValidationTrace, application):
        self.trace = trace
        self.application = application

    def install(self) -> None:
        self.application.configure_telemetry(self.on_event)

    @staticmethod
    def _token_fields(token) -> dict[str, int]:
        if token is None:
            return {}
        return {'session': token.session, 'line': token.line}

    def _emit_hook(self, event, outcome) -> None:
        if outcome.connection_transition in ('connected', 'changed'):
            self.trace.emit(
                'hook_connected', pid=event.process_id,
                generation=event.host_generation, connection=event.connection_generation)

        if outcome.capture.changed and event.source is not None:
            if event.kind == 'created':
                self.trace.emit(
                    'hook_candidate_created', pid=event.process_id,
                    generation=event.host_generation, connection=event.connection_generation,
                    source=event.source.identity, hook_sequence=event.sequence)
            elif event.kind == 'output':
                self.trace.emit(
                    'hook_candidate_output', pid=event.process_id,
                    generation=event.host_generation, connection=event.connection_generation,
                    source=event.source.identity, hook_sequence=event.sequence,
                    raw_hash=text_digest(event.text))

        if event.kind == 'diagnostic':
            self.trace.emit(
                'hook_native_diagnostic', pid=event.process_id,
                generation=event.host_generation, connection=event.connection_generation,
                info_type=event.detail.partition(':')[0][:64],
                message_hash=text_digest(event.detail))

    def on_event(self, event: str, token, detail: dict) -> None:
        if event == 'selected_hook_output':
            fields = dict(detail)
            raw = fields.pop('raw', '')
            self.trace.emit('hook_output', **fields, raw_hash=text_digest(raw))
            return

        if event == 'hook_event':
            self._emit_hook(detail['hook_event'], detail['outcome'])
            return

        if event == 'utterance' and token is not None:
            utterance = self.application.runtime_state().utterance
            if utterance is None or self.application.runtime_state().token != token:
                return
            self.trace.emit(
                'utterance', session=token.session, line=token.line,
                source=utterance.source, hook_sequence=utterance.sequence,
                raw_hash=text_digest(utterance.raw), body_hash=text_digest(utterance.text),
                speaker=utterance.speaker, advance_mode=utterance.advance_mode.value,
                use_tts=utterance.use_tts, reading_units=utterance.reading_unit_count,
                text_wait_ms=utterance.text_wait_ms,
                uncertain_text_fallback=utterance.uncertain_text_fallback)
            return

        if event == 'native_ready':
            if detail.get('ready'):
                self.trace.emit(
                    'native_ready', session=detail.get('session', 0),
                    audio_pid=detail.get('audio_pid', 0))
            return

        if event == 'native_voice_knowledge' and token is not None:
            self.trace.emit(
                'native_voice_knowledge', session=token.session, line=token.line,
                target_associated=bool(detail.get('target_associated')),
                target_acoustic_completed=bool(detail.get('target_acoustic_completed')),
                completion_source=detail.get('completion_source', ''))
            return

        fields = dict(detail)
        fields.update(self._token_fields(token))
        if event == 'presentation_complete':
            fields['success'] = True
        if event == 'monitor_ended':
            fields.setdefault('session', detail.get('session', 0))
        if event == 'audio_observation_boundary' and token is not None:
            fields.pop('line', None)
            fields['after_line'] = token.line
        if event in {
            'monitor_started', 'monitor_ended', 'external_hook_transition',
            'presentation_complete', 'next_line_timeout', 'advance_attempt',
            'advance_input', 'audio_observation_boundary', 'audio_deadline_elapsed',
            'native_capture_summary', 'native_audio_error',
            'native_pcm', 'alignment_inference', 'native_audio_observation', 'native_target_acoustic_completion_estimate',
            'tts_timing', 'tts_result', 'controller_handoff',
        }:
            self.trace.emit(event, **fields)
