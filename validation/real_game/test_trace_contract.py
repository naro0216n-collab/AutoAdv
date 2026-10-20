"""Acceptance evidence must fail closed; generated traces are not real E2E."""
import json
from pathlib import Path
import tempfile
import unittest
from autoadv.validation_trace import ValidationTrace, text_digest
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

    def test_no_actual_autoadv_events_is_always_unverified(self):
        result = validate([])
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        only_header = [{"event": "trace_started", "at_ns": 1}]
        result = validate(only_header)
        self.assertFalse(result["ok"])
        self.assertIn("no live dialogue observed", result["errors"])


if __name__ == "__main__":
    unittest.main()
