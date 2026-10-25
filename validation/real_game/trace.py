"""Validation evidence writer.

The validation layer records product facts; it does not turn those facts into a
second success/failure state machine.  Raw selected Hook text and the actual
Utterance produced from it are intentionally retained so a remote analysis can
reconstruct framing/merging failures without access to the Windows run folder.
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
import time


class ValidationTrace:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._sequence = 0
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps({
                'event': 'trace_started', 'version': 3, 'at_ns': time.monotonic_ns()
            }, ensure_ascii=False, separators=(',', ':')) + '\n')

    def emit(self, event: str, **fields: object) -> None:
        with self._lock:
            self._sequence += 1
            item = {'event': event, 'seq': self._sequence, 'at_ns': time.monotonic_ns(), **fields}
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')


class ProductTraceObserver:
    """Persist selected product facts without validation-side interpretation."""

    _PASSTHROUGH = frozenset({
        'monitor_started', 'monitor_ended', 'external_hook_transition',
        'presentation_complete', 'next_line_timeout', 'advance_attempt',
        'advance_input', 'audio_causal_lower_bound', 'audio_input_lower_bound',
        'game_input_observed', 'game_input_observer_error', 'audio_deadline_elapsed',
        'native_capture_summary', 'native_audio_error', 'native_pcm',
        'alignment_inference', 'native_audio_observation', 'utterance_progress',
        'tts_timing', 'tts_result', 'controller_handoff',
    })

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

    def on_event(self, event: str, token, detail: dict) -> None:
        if event == 'selected_hook_output':
            fields = dict(detail)
            self.trace.emit('hook_output', **fields)
            return

        if event == 'utterance' and token is not None:
            runtime = self.application.runtime_state()
            utterance = runtime.utterance
            if utterance is None or runtime.token != token:
                return
            self.trace.emit(
                'utterance', session=token.session, line=token.line,
                source=utterance.source, hook_sequence=utterance.sequence,
                raw=utterance.raw, body=utterance.text, speaker=utterance.speaker,
                advance_mode=utterance.advance_mode.value, use_tts=utterance.use_tts,
                uncertain_text_fallback=utterance.uncertain_text_fallback,
                reading_units=utterance.reading_unit_count, text_wait_ms=utterance.text_wait_ms)
            return

        if event == 'native_ready':
            if detail.get('ready'):
                self.trace.emit(
                    'native_ready', session=detail.get('session', 0),
                    audio_pid=detail.get('audio_pid', 0))
            return

        if event not in self._PASSTHROUGH:
            return
        fields = dict(detail)
        fields.update(self._token_fields(token))
        if event == 'monitor_ended':
            fields.setdefault('session', detail.get('session', 0))
        if event == 'audio_causal_lower_bound' and token is not None:
            fields.pop('line', None)
            fields['after_line'] = token.line
        self.trace.emit(event, **fields)
