from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from validation.real_game import issue_reporting


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "validation" / "real_game" / "run_windows_validation.ps1"


def _phase(name: str, *, report_present: bool = True, eligible_accuracy: bool = False) -> dict:
    return {
        "phase": name,
        "collection_valid": True,
        "measurement_valid": True,
        "collection_complete": True,
        "advancement_observed": True,
        "automatic_continuation_verified": True,
        "validation_recovery_inputs": 0,
        "voice_safety_verified": True if eligible_accuracy else None,
        "quality": {
            "requested_cases": 24 if name == "accuracy" else 3,
            "observed_cases": 24 if name == "accuracy" else 3,
            "automatic_advance_rate": 1.0,
            "safe_automatic_advance_rate": 1.0,
            "process_audio_observation_rate": 1.0 if name == "accuracy" else None,
            "alignment_observation_rate": 1.0 if name == "accuracy" else None,
            "mix_environment_observation_rate": 1.0 if name == "accuracy" else None,
            "foreground_native_completion_rate": 1.0 if name == "accuracy" else None,
            "recovery_rate": 0.0,
            "early_input_rate": 0.0 if name == "accuracy" else None,
            "overhold_rate": 0.0 if name == "accuracy" else None,
            "misattribution_rate": 0.0 if name == "accuracy" else None,
        },
        "quality_issues": [],
        "report_present": report_present,
        "runner_error": None,
    }


def _write_summary(root: Path, *, accuracy: dict | None = None) -> None:
    phases = []
    if accuracy is not None:
        phases.append(accuracy)
    payload = {
        "schema": "autoadv-local-validation-v3",
        "repository": "naro0216n-collab/AutoAdv",
        "branch": "develop",
        "commit": "a" * 40,
        "started_utc": "2026-10-23T13:41:56Z",
        "ended_utc": "2026-10-23T13:45:00Z",
        "phase_requested": "accuracy",
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


class _FakeIssueClient:
    def __init__(self, comments: list[dict], *, fail_role: str | None = None):
        self.comments = {int(comment["id"]): dict(comment) for comment in comments}
        self.fail_role = fail_role
        self.patched: list[int] = []

    def list_comments(self) -> list[dict]:
        return list(self.comments.values())

    def patch_comment(self, comment_id: int, body: str) -> dict:
        role = "baseline" if body.startswith(issue_reporting.BASELINE_MARKER) else "history"
        if role == self.fail_role:
            raise RuntimeError(f"synthetic {role} failure")
        self.comments[comment_id]["body"] = body
        self.patched.append(comment_id)
        return {
            **self.comments[comment_id],
            "html_url": f"https://github.com/naro0216n-collab/AutoAdv/issues/2#issuecomment-{comment_id}",
        }

    def get_comment(self, comment_id: int) -> dict:
        return dict(self.comments[comment_id])


class IssueReportingContract(unittest.TestCase):
    def test_snapshot_accepts_missing_optional_accuracy_line_fields(self):
        with tempfile.TemporaryDirectory(prefix="20261023T134156Z-b10d6904-") as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, accuracy=_phase("accuracy"))
            report = {
                "performance": {},
                "line_results": [{
                    "scenario": "room-clean-short",
                    "speaker_family": "amitaro",
                    "audio_environment": "clean_room_short",
                    "outcome": "no_autoadv_input",
                    "completion_reason": None,
                    "pcm_observed": True,
                    "alignment_inference_count": 4,
                    # game_outcome and game_end_to_input_ms are intentionally absent.
                }],
                "quality_issues": [],
                "errors": [],
                "diagnostics": ["voice-end not independently observed"],
            }
            (root / "autoadv-accuracy.result.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )

            snapshot = issue_reporting.format_snapshot(root, "a" * 40)

            self.assertIn("| room-clean-short | - | amitaro | clean_room_short | - | no_autoadv_input |", snapshot.body)
            self.assertIn("| yes | 4 | - | - |", snapshot.body)
            self.assertIn("voice-end not independently observed", snapshot.body)

    def test_setup_failure_without_accuracy_report_is_still_uploadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            accuracy = _phase("accuracy", report_present=False)
            accuracy["measurement_valid"] = None
            accuracy["collection_valid"] = None
            accuracy["collection_complete"] = None
            accuracy["runner_error"] = "Accuracy setup failed: python failed (exit code 1)"
            accuracy["quality"] = None
            _write_summary(root, accuracy=accuracy)
            (root / "validation.log").write_text(
                "noise\n"
                "Advancement accuracy media asset: cache\n"
                "WARNING token github_pat_1234567890abcdef path C:\\Users\\private\\x\n"
                "RuntimeError: failed\n",
                encoding="utf-8",
            )

            snapshot = issue_reporting.format_snapshot(root, "b" * 40)

            self.assertIn("setup diagnostics", snapshot.body)
            self.assertIn("Advancement accuracy media asset: cache", snapshot.body)
            self.assertIn("<REDACTED-TOKEN>", snapshot.body)
            self.assertIn("<USERPROFILE>\\x", snapshot.body)
            self.assertNotIn("C:\\Users\\private", snapshot.body)
            self.assertNotIn("github_pat_1234567890abcdef", snapshot.body)

    def test_fixed_history_is_a_ring_and_patches_only_the_oldest_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, accuracy=_phase("accuracy"))

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

    def test_eligible_accuracy_run_adds_only_the_independent_baseline_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, accuracy=_phase("accuracy", eligible_accuracy=True))

            plan = issue_reporting.build_upload_plan(root, "d" * 40, _comments())

            self.assertEqual([op["role"] for op in plan["operations"]], ["history", "baseline"])
            self.assertEqual(plan["operations"][1]["comment_id"], 2000)

    def test_missing_audio_coverage_cannot_replace_known_good_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            accuracy = _phase("accuracy", eligible_accuracy=True)
            accuracy["quality"]["alignment_observation_rate"] = 0.9565
            _write_summary(root, accuracy=accuracy)

            plan = issue_reporting.build_upload_plan(root, "e" * 40, _comments())

            self.assertEqual([op["role"] for op in plan["operations"]], ["history"])

    def test_missing_game_mix_coverage_cannot_replace_known_good_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            accuracy = _phase("accuracy", eligible_accuracy=True)
            accuracy["quality"]["mix_environment_observation_rate"] = 0.9583
            _write_summary(root, accuracy=accuracy)

            plan = issue_reporting.build_upload_plan(root, "f" * 40, _comments())

            self.assertEqual([op["role"] for op in plan["operations"]], ["history"])

    def test_paginated_gh_slurp_output_is_flattened_before_planning(self):
        comments = _comments()
        flattened = issue_reporting._flatten_comments([comments[:2], comments[2:]])
        self.assertEqual(flattened, comments)

    def test_duplicate_fixed_marker_is_an_error(self):
        comments = _comments()
        comments.append({"id": 9999, "body": f"{issue_reporting.history_marker(2)}\nduplicate"})
        with self.assertRaisesRegex(ValueError, "slot-2.*2"):
            issue_reporting.choose_history_slot(comments, "20261023T134156Z-b10d6904")

    def test_same_run_in_multiple_history_slots_is_an_error(self):
        comments = _comments()
        run = "20261023T134156Z-b10d6904"
        comments[0]["body"] = f"{issue_reporting.history_marker(1)}\n<!-- autoadv-validation-run:{run} -->"
        comments[1]["body"] = f"{issue_reporting.history_marker(2)}\n<!-- autoadv-validation-run:{run} -->"
        with self.assertRaisesRegex(ValueError, "multiple history slots"):
            issue_reporting.choose_history_slot(comments, run)

    def test_duplicate_baseline_is_an_error_even_for_nonbaseline_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, accuracy=_phase("accuracy", eligible_accuracy=False))
            comments = _comments()
            comments.append({"id": 9999, "body": f"{issue_reporting.BASELINE_MARKER}\nduplicate"})
            with self.assertRaisesRegex(ValueError, "baseline.*2"):
                issue_reporting.build_upload_plan(root, "a" * 40, comments)

    def test_missing_fixed_marker_is_an_error(self):
        comments = [comment for comment in _comments() if comment["id"] != 1004]
        with self.assertRaisesRegex(ValueError, "slot-4.*0"):
            issue_reporting.choose_history_slot(comments, "20261023T134156Z-b10d6904")

    def test_reporter_patches_and_verifies_history_without_shell_body_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, accuracy=_phase("accuracy"))
            client = _FakeIssueClient(_comments())

            result = issue_reporting.publish_snapshot(root, "a" * 40, client)

            self.assertEqual(client.patched, [1004])
            self.assertEqual(result["operations"][0]["role"], "history")
            self.assertEqual(
                issue_reporting._run_from_body(client.comments[1004]["body"]), root.name
            )

    def test_baseline_failure_preserves_verified_history_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20261023T134156Z-b10d6904"
            root.mkdir()
            _write_summary(root, accuracy=_phase("accuracy", eligible_accuracy=True))
            client = _FakeIssueClient(_comments(), fail_role="baseline")

            with self.assertRaises(issue_reporting.IssuePublishError) as raised:
                issue_reporting.publish_snapshot(root, "a" * 40, client)

            self.assertEqual(client.patched, [1004])
            self.assertEqual(raised.exception.completed[0]["role"], "history")
            slot, comment_id = issue_reporting.choose_history_slot(
                list(client.comments.values()), root.name
            )
            self.assertEqual((slot, comment_id), (4, 1004))

    def test_github_cli_writes_non_ascii_issue_body_as_utf8_request_file(self):
        captured: dict[str, str] = {}

        def fake_run(command, **kwargs):
            request_path = Path(command[command.index("--input") + 1])
            captured.update(json.loads(request_path.read_text(encoding="utf-8")))
            return type("Completed", (), {
                "returncode": 0,
                "stdout": json.dumps({"id": 1004, "html_url": "https://github.com/x/y"}).encode("utf-8"),
                "stderr": b"",
            })()

        client = issue_reporting.GitHubCliIssueClient(
            Path("gh.exe"), "naro0216n-collab/AutoAdv", 2
        )
        with patch.object(issue_reporting.subprocess, "run", side_effect=fake_run):
            client.patch_comment(1004, "### run · 日本語")

        self.assertEqual(captured["body"], "### run · 日本語")

    def test_windows_runner_delegates_entire_issue_transaction_to_python(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("validation.real_game.issue_reporting", source)
        uploader = source[source.index("function Upload-Result"):source.index("function Get-PrebuiltFixture")]
        self.assertIn("Ensure-GitHubLogin", uploader)
        self.assertIn("& $pythonExecutable -m validation.real_game.issue_reporting", uploader)
        self.assertIn("--repository $repository --issue $IssueNumber", uploader)
        self.assertIn("--gh $script:GitHubCli --status $statusPath", uploader)
        self.assertIn("finally { Pop-Location }", uploader)
        self.assertIn("Upload-Result $session $gitSha $python", source)
        self.assertNotIn("Get-IssueUploadPlan", source)
        self.assertNotIn("Invoke-GitHubIssueApi", source)
        self.assertNotIn("Set-IssueComment", source)
        self.assertNotIn("ConvertFrom-Json", uploader)
        self.assertNotIn("--comments", uploader)


if __name__ == "__main__":
    unittest.main()
