"""Acceptance evidence must fail closed; generated traces are not real E2E."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.application import HookEventOutcome, CaptureHostEventOutcome
from autoadv.conversation import Token
from validation.real_game.trace import ProductTraceObserver, ValidationTrace, text_digest
from validation.real_game.verify_trace import validate


class EvidenceIntegrityTests(unittest.TestCase):
    def test_trace_opt_in_writes_hashed_records_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = ValidationTrace(str(path))
            trace.emit("utterance", session=3, line=1, body_hash=text_digest("えっ嘘でしょ。"))
            data = path.read_text(encoding="utf-8")
            self.assertNotIn("えっ嘘でしょ。", data)
            records = [json.loads(v) for v in data.splitlines()]
            self.assertEqual([r["event"] for r in records], ["trace_started", "utterance"])
            self.assertTrue(records[-1]["at_ns"] >= records[0]["at_ns"])
            with self.assertRaises(FileExistsError):
                ValidationTrace(str(path))

    def test_trace_keeps_raw_audio_observation_separate_from_voice_knowledge(self):
        class Application:
            def configure_telemetry(self, observer):
                self.observer = observer

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.jsonl'
            app = Application()
            observer = ProductTraceObserver(ValidationTrace(path), app)
            observer.install()
            token = Token(4, 2)
            app.observer('native_audio_observation', token, {
                'kind': 'asr', 'matched_phonemes': 3,
                'phonetic_target_units': 4, 'phonetic_prefix_units': 3,
                'phonetic_edit_count': 0, 'vad_tail_ms': 0,
            })
            app.observer('native_voice_knowledge', token, {
                'target_associated': True, 'target_acoustic_completed': False,
                'completion_source': '',
            })
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            raw = next(record for record in records if record['event'] == 'native_audio_observation')
            knowledge = next(record for record in records if record['event'] == 'native_voice_knowledge')
            self.assertEqual(raw['matched_phonemes'], 3)
            self.assertNotIn('target_acoustic_completed', raw)
            self.assertTrue(knowledge['target_associated'])
            self.assertFalse(knowledge['target_acoustic_completed'])
            self.assertNotIn('matched_phonemes', knowledge)

    def test_no_actual_autoadv_events_is_always_unverified(self):
        result = validate([])
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        only_header = [{"event": "trace_started", "at_ns": 1}]
        result = validate(only_header)
        self.assertFalse(result["ok"])
        self.assertIn("no live dialogue observed", result["errors"])

    def test_validation_serializes_only_application_selected_hook_provenance_as_hook_output(self):
        class Application:
            def configure_telemetry(self, observer):
                self.observer = observer

            def runtime_state(self):
                return SimpleNamespace(token=None, utterance=None)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.jsonl'
            app = Application()
            observer = ProductTraceObserver(ValidationTrace(path), app)
            observer.install()
            source = ThreadSource(50, 0x100, 1, 2, 'T', 'T')
            output = HookEvent('output', 'host-a', 7, 3, 30, 50, source, '選択台詞')

            app.observer(
                'selected_hook_output', None,
                {'session': 2, 'generation': 'host-a', 'connection': 7,
                 'hook_sequence': 3, 'source': source.identity, 'received_ns': 30,
                 'pid': 50, 'address': source.address, 'context': source.context,
                 'context2': source.context2, 'raw': output.text})
            # A generic candidate notification must not be reclassified by the
            # validation observer as selected runtime provenance.
            app.observer(
                'hook_event', None,
                {'hook_event': output,
                 'outcome': HookEventOutcome(capture=CaptureHostEventOutcome(changed=True))})

            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            hook_outputs = [record for record in records if record['event'] == 'hook_output']
            candidates = [record for record in records if record['event'] == 'hook_candidate_output']
            self.assertEqual(len(hook_outputs), 1)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(hook_outputs[0]['raw_hash'], text_digest('選択台詞'))
            self.assertNotIn('raw', hook_outputs[0])


if __name__ == "__main__":
    unittest.main()
