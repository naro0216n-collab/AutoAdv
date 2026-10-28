"""Validation evidence writer.

The validation layer records product telemetry facts; it does not turn those
facts into a second success/failure state machine. Raw selected Hook text, the
actual Utterance, acoustic observations and service/runtime diagnostics are
retained so remote analysis does not depend on the Windows run folder.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
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
    """Persist product telemetry facts without validation-side interpretation."""

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

    @classmethod
    def _json_value(cls, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Enum):
            return cls._json_value(value.value)
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value):
            return cls._json_value(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._json_value(item) for item in value]
        return repr(value)

    @classmethod
    def _safe_fields(cls, detail: dict) -> dict:
        fields = {str(key): cls._json_value(value) for key, value in detail.items()}
        # Preserve producer data without allowing it to overwrite the trace
        # envelope owned by ValidationTrace.
        for key in ('event', 'seq', 'at_ns'):
            if key in fields:
                fields[f'detail_{key}'] = fields.pop(key)
        return fields

    def on_event(self, event: str, token, detail: dict) -> None:
        if event == 'selected_hook_output':
            fields = self._safe_fields(detail)
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
                input_method=utterance.input_method.value, input_key=utterance.input_key,
                uncertain_text_fallback=utterance.uncertain_text_fallback,
                force_advance_seconds=utterance.force_advance_seconds,
                reading_units=utterance.reading_unit_count, text_wait_ms=utterance.text_wait_ms)
            return

        fields = self._safe_fields(detail)
        fields.update(self._token_fields(token))
        if event == 'monitor_ended':
            fields.setdefault('session', detail.get('session', 0))
        if event == 'audio_causal_lower_bound' and token is not None:
            fields['after_line'] = token.line
        self.trace.emit(event, **fields)
