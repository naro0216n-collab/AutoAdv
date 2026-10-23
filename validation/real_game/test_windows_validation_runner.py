from pathlib import Path


SCRIPT = Path(__file__).with_name("run_windows_validation.ps1")


def _upload_result_body() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("function Invoke-GitHubIssueApi")
    end = source.index("function Get-PrebuiltFixture", start)
    return source[start:end]


def test_issue_upload_rotates_four_fixed_history_slots() -> None:
    body = _upload_result_body()

    assert "autoadv-validation-slot-$slot" in body
    assert "foreach ($slot in 1..4)" in body
    assert "for ($slot = 4; $slot -ge 2; $slot--)" in body
    assert "--method',$method" in body
    assert "PATCH" in body
    assert "validation run never adds unbounded issue history" in body


def test_issue_upload_preserves_known_good_native_baseline() -> None:
    body = _upload_result_body()

    assert "autoadv-validation-baseline" in body
    assert "Test-NativeBaselineEligible" in body
    for field in (
        "measurement_valid",
        "collection_complete",
        "validation_recovery_inputs",
        "voice_safety_verified",
        "runner_error",
    ):
        assert field in body


def test_issue_snapshot_keeps_comparison_and_remote_diagnostics() -> None:
    body = _upload_result_body()

    for field in (
        "requested_cases",
        "observed_cases",
        "automatic_advance_rate",
        "safe_automatic_advance_rate",
        "recovery_rate",
        "early_input_rate",
        "overhold_rate",
        "misattribution_rate",
        "line_results",
        "speaker_family",
        "audio_environment",
        "asr_inference_count",
        "game_end_to_input_ms",
        "quality_issues",
        "errors",
        "diagnostics",
    ):
        assert field in body
    assert "setup diagnostics" in body
    assert "RealAudio media|Native setup failed|Traceback" in body
    assert "autoadv-(reading|native)" not in body
