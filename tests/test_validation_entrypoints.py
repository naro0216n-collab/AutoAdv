import ast
from pathlib import Path
from pathlib import PureWindowsPath
import unittest

from validation.real_game import fixture_runtime

ROOT = Path(__file__).resolve().parents[1]


class WindowsValidationLauncherContract(unittest.TestCase):
    def test_repo_owned_launcher_uses_current_prebuilt_product_path(self):
        source = (ROOT / 'validation' / 'real_game' / 'run_windows_validation.ps1').read_text(encoding='utf-8')
        self.assertIn('validation.real_game.fixture_runtime stage', source)
        self.assertNotIn('autoadv-validation-fixtures', source)
        self.assertNotIn('Invoke-WebRequest', source)
        self.assertIn('validation.real_game.prepare_asr_model', source)
        self.assertIn('AutoAdv.ProcessLoopback.csproj', source)
        self.assertIn("'-GameExecutable',$gameExecutable", source)
        self.assertNotIn('RenPySdk', source)
        self.assertNotIn('renpy-8.4.1-sdk', source)


    def test_publish_gateway_requests_fixture_build_only_for_fixture_input_changes(self):
        source = (ROOT / '.github' / 'workflows' / 'publish-gateway.yml').read_text(encoding='utf-8')
        self.assertIn('validation-fixture.yml/dispatches', source)
        self.assertIn('git diff --quiet "$TARGET_BEFORE" "$PUBLISH_COMMIT"', source)
        self.assertIn('validation/real_game/renpy validation/real_game/fixture_runtime.py', source)
        self.assertIn("if: env.TARGET_BRANCH == 'develop'", source)

    def test_fixture_identity_uses_host_independent_path_order(self):
        source = fixture_runtime.SOURCE
        canonical = [relative.as_posix() for relative, _ in fixture_runtime._iter_source_files(source)]
        self.assertEqual(canonical, sorted(canonical))

        # The old implementation sorted Path objects directly. WindowsPath
        # folds case and therefore places game/... before LICENSE_*.txt, while
        # PosixPath does not. Keep this distinction in the fixture so the test
        # actually protects the cross-platform bug that broke Live validation.
        windows_order = [
            name for name in sorted(canonical, key=lambda value: PureWindowsPath(value))
        ]
        self.assertNotEqual(canonical, windows_order)
        self.assertEqual(
            fixture_runtime.source_digest(source),
            fixture_runtime._tree_digest(
                sorted(
                    [(relative, path) for relative, path in fixture_runtime._iter_source_files(source)],
                    key=lambda item: item[0].as_posix(),
                )
            ),
        )

    def test_cmd_is_only_a_develop_bootstrap(self):
        source = (ROOT / 'AutoAdv-Windows-Validate.cmd').read_text(encoding='utf-8')
        self.assertIn('--branch develop', source)
        self.assertIn('-WorkBase $base', source)
        self.assertIn('validation\\real_game\\run_windows_validation.ps1', source)
        for stale in ('RenPySdk', 'prepare_asr_model', 'AutoAdv.ProcessLoopback.csproj', 'run_live_check.ps1'):
            self.assertNotIn(stale, source)

    def test_realaudio_is_one_continuous_native_session_with_marked_post_failure_recovery(self):
        orchestrator = (ROOT / 'validation' / 'real_game' / 'run_windows_validation.ps1').read_text(encoding='utf-8')
        live = (ROOT / 'validation' / 'real_game' / 'run_live_check.ps1').read_text(encoding='utf-8')
        fixture = (ROOT / 'validation' / 'real_game' / 'renpy' / 'game' / 'script.rpy').read_text(encoding='utf-8')
        self.assertNotIn('case_matrix', orchestrator)
        self.assertNotIn('NativeCase', live)
        self.assertNotIn('AUTOADV_VALIDATION_CASE', fixture)
        self.assertIn("'native' { 21 }", live)
        self.assertIn("continue_collection = ($Phase -eq 'native')", live)
        self.assertIn("if ($MeasurementOnly) { $arguments += '--measurement-only' }", live)
        self.assertIn("'-Automated','-MeasurementOnly'", orchestrator)
        self.assertIn('prepare_real_audio_assets', orchestrator)
        self.assertIn("Join-Path $cache 'media'", orchestrator)
        self.assertNotIn('skipped_after_reading_hook_failure', orchestrator)
        self.assertIn('$_.measurement_valid -ne $true', orchestrator)
        self.assertNotIn('$_.voice_safety_verified -ne $true', orchestrator)
        self.assertNotIn('$_.automatic_continuation_verified -ne $true', orchestrator)
        driver = (ROOT / 'validation' / 'real_game' / 'gui_session.py').read_text(encoding='utf-8')
        self.assertIn("'validation_recovery_input'", driver)
        self.assertIn('confirm_external_advance', driver)
        self.assertIn('collection_complete = if ($measurement)', orchestrator)
        self.assertIn('automatic_continuation_verified = if ($measurement)', orchestrator)
        self.assertIn('validation_recovery_inputs = if ($measurement)', orchestrator)
        self.assertIn('jump continuation', fixture)
        self.assertNotIn('autoadv_case_', fixture)


    def test_product_entrypoint_has_no_validation_driver_or_test_only_runtime_wrappers(self):
        main = (ROOT / 'autoadv' / '__main__.py').read_text(encoding='utf-8')
        gui = (ROOT / 'autoadv' / 'gui.py').read_text(encoding='utf-8')
        application = (ROOT / 'autoadv' / 'application.py').read_text(encoding='utf-8')
        live = (ROOT / 'validation' / 'real_game' / 'run_live_check.ps1').read_text(encoding='utf-8')

        self.assertNotIn('AUTOADV_VALIDATION_SESSION', main)
        self.assertNotIn('validation.real_game', main)
        self.assertNotIn('session_driver', gui)
        self.assertIn('python -m validation.real_game.gui_entry', live)
        tree = ast.parse(application)
        application_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == 'AutoAdvApplication')
        methods = {node.name for node in application_class.body if isinstance(node, ast.FunctionDef)}
        self.assertIn('confirm_external_advance', methods)
        for retired in (
            'stage_runtime_profile', 'hold_runtime',
            'observe_runtime_hook', 'observe_runtime_group',
            'observe_runtime_ocr', 'expire_next_line', 'try_advance',
            'native_audio_boundary', 'hook_connection_state',
            'observe_capture_group', 'capture_has_identity',
            'capture_has_output', 'rebase_profile_settings',
            'take_external_transition', 'adopt_inserted_hook',
        ):
            self.assertNotIn(retired, methods)

    def test_hosted_and_broad_ci_do_not_run_real_audio(self):
        live = (ROOT / '.github' / 'workflows' / 'live-advancement.yml').read_text(encoding='utf-8')
        self.assertIn('-Phase reading', live)
        self.assertIn('validation.real_game.fixture_runtime stage', live)
        self.assertNotIn('gh release download', live)
        self.assertNotIn('autoadv-validation-fixtures', live)
        for forbidden in ('-Phase native', 'prepare_asr_model', 'AutoAdv.ProcessLoopback.csproj'):
            self.assertNotIn(forbidden, live)

        fixture = (ROOT / '.github' / 'workflows' / 'validation-fixture.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', fixture)
        self.assertNotIn('pull_request:', fixture)
        self.assertNotIn('[BUILD AUTOADV FIXTURE]', fixture)

        for name in ('ci.yml', 'windows-adapters.yml'):
            source = (ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8')
            self.assertNotIn('validation.real_game.test_native_audio', source)
            self.assertNotIn('run_live_check.ps1', source)


if __name__ == '__main__':
    unittest.main()
