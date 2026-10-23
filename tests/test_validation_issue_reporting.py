from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from validation.real_game import issue_reporting


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "validation" / "real_game" / "run_windows_validation.ps1"


def _phase(name: str, *, report_present: bool = True, eligible_native: bool = False) -> dict:
    return {
        "phase": name,
        "collection_valid": True,
        "measurement_valid": True,
        "collection_complete": True,
        "advancement_observed": True,
        "automatic_continuation_verified": True,
        "validation_recovery_inputs": 0,
        "voice_safety_verified": True if eligible_native else None,
        "quality": {
            "requested_cases": 21 if name == "native" else 3,
            "observed_cases": 21 if name == "native" else 3,
            "automatic_advance_rate": 1.0,
            "safe_automatic_advance_rate": 1.0,
            "recovery_rate": 0.0,
            "early_input_rate": 0.0 if name == "native" else None,
            "overhold_rate": 0.0 if name == "native" else None,
            "misattribution_rate": 0.0 if name == "native" else None,
        },
        "quality_issues": [],
        "report_present": report_present,
        "runner_error": None,
    }


def _write_summary(root: Path, *, native: dict | None = None) -> None:
    phases = [_phase("reading")]
    if native is not None:
        phases.append(native)
    payload = {
        "schema": "autoadv-local-validation-v3",
        "repository": "naro0216n-collab/AutoAdv",
        "branch": "develop",
        "commit": "a" * 40,
        "started_utc": "2026-10-23T13:41:56Z",
        "ended_utc": "2026-10-23T13:45:00Z",
        "phase_requested": "all",
        "phases": phases,
        "launcher_error": None,
    }
    (root / "validation-summary.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _comments() -> list[dict]:
    runs = {
        1: "20261023T130049Z-36c5ea8d",
        2: "20261023T102922Z-d01111ed",
        3: "20261023T095133Z-4fdc7015",
        4: "20261023T014504Z-0b2c7d31",
    }
    comments = [
        {
            "id": 1000 + slot,
            "body": (
                f"{issue_reporting.history_marker(slot)}\n"
                f"<!-- autoadv-validation-run:{run} -->\nold"
            ),
        }
        for slot, run in runs.items()
    ]
    comments.append({"id": 2000, "body": f"{issue_reporting.BASELINE_MARKER}\nold baseline"})
    return comments


class IssueReportingContract(unittest.TestCase):
    def test_snapshot_accepts_missing_optional_native_line_fields(self):
        with tempfile.TemporaryDirectory(prefix="20261023T134156Z-b10d6904-") as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, native=_phase("native"))
            report = {
                "performance": {},
                "line_results": [{
                    "scenario": "clean-local-short",
                    "speaker_family": "amitaro",
                    "audio_environment": "clean",
                    "outcome": "no_autoadv_input",
                    "completion_reason": None,
                    "asr_inference_count": 4,
                    # game_outcome and game_end_to_input_ms are intentionally absent.
                }],
                "quality_issues": [],
                "errors": [],
                "diagnostics": ["voice-end not independently observed"],
            }
            (root / "autoadv-native.result.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )

            snapshot = issue_reporting.format_snapshot(root, "a" * 40)

            self.assertIn("| clean-local-short | amitaro | clean | no_autoadv_input |", snapshot.body)
            self.assertIn("| 4 | - | - |", snapshot.body)
            self.assertIn("voice-end not independently observed", snapshot.body)

    def test_setup_failure_without_native_report_is_still_uploadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            native = _phase("native", report_present=False)
            native["measurement_valid"] = None
            native["collection_valid"] = None
            native["collection_complete"] = None
            native["runner_error"] = "Native setup failed: python failed (exit code 1)"
            native["quality"] = None
            _write_summary(root, native=native)
            (root / "validation.log").write_text(
                "noise\n"
                "RealAudio media asset: cache\n"
                "WARNING token github_pat_1234567890abcdef path C:\\Users\\private\\x\n"
                "RuntimeError: failed\n",
                encoding="utf-8",
            )

            snapshot = issue_reporting.format_snapshot(root, "b" * 40)

            self.assertIn("setup diagnostics", snapshot.body)
            self.assertIn("RealAudio media asset: cache", snapshot.body)
            self.assertIn("<REDACTED-TOKEN>", snapshot.body)
            self.assertIn("<USERPROFILE>\\x", snapshot.body)
            self.assertNotIn("C:\\Users\\private", snapshot.body)
            self.assertNotIn("github_pat_1234567890abcdef", snapshot.body)

    def test_fixed_history_is_a_ring_and_patches_only_the_oldest_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, native=_phase("native"))

            plan = issue_reporting.build_upload_plan(root, "c" * 40, _comments())

            self.assertEqual(plan["run"], root.name)
            self.assertEqual(len(plan["operations"]), 1)
            operation = plan["operations"][0]
            self.assertEqual(operation["role"], "history")
            self.assertEqual(operation["slot"], 4)
            self.assertEqual(operation["comment_id"], 1004)
            self.assertTrue(operation["body"].startswith(issue_reporting.history_marker(4)))

    def test_retry_rewrites_the_same_slot_without_consuming_history(self):
        comments = _comments()
        comments[2]["body"] = (
            f"{issue_reporting.history_marker(3)}\n"
            "<!-- autoadv-validation-run:20261023T134156Z-b10d6904 -->\npartial"
        )
        slot, comment_id = issue_reporting.choose_history_slot(
            comments, "20261023T134156Z-b10d6904"
        )
        self.assertEqual((slot, comment_id), (3, 1003))

    def test_eligible_native_run_adds_only_the_independent_baseline_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, native=_phase("native", eligible_native=True))

            plan = issue_reporting.build_upload_plan(root, "d" * 40, _comments())

            self.assertEqual([op["role"] for op in plan["operations"]], ["history", "baseline"])
            self.assertEqual(plan["operations"][1]["comment_id"], 2000)

    def test_windows_runner_delegates_testable_planning_and_has_no_rotation_loop(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("validation.real_game.issue_reporting", source)
        self.assertIn("foreach ($operation in @($plan.operations))", source)
        self.assertNotIn("for ($slot = 4; $slot -ge 2; $slot--)", source)
        self.assertNotIn("Format-IssueSnapshot", source)
        self.assertIn("PATCH", source)


if __name__ == "__main__":
    unittest.main()
