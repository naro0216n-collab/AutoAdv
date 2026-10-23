from pathlib import Path
from pathlib import PureWindowsPath
import unittest

from validation.real_game import fixture_runtime

ROOT = Path(__file__).resolve().parents[1]


class WindowsValidationLauncherContract(unittest.TestCase):
    def test_repo_owned_launcher_uses_current_prebuilt_product_path(self):
        source = (ROOT / 'validation' / 'real_game' / 'run_windows_validation.ps1').read_text(encoding='utf-8')
        self.assertIn('validation.real_game.fixture_runtime source-digest', source)
        self.assertIn('autoadv-validation-fixtures', source)
        self.assertIn('validation.real_game.prepare_asr_model', source)
        self.assertIn('AutoAdv.ProcessLoopback.csproj', source)
        self.assertIn("'-GameExecutable',$gameExecutable", source)
        self.assertNotIn('RenPySdk', source)
        self.assertNotIn('renpy-8.4.1-sdk', source)


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

    def test_realaudio_is_one_continuous_native_session_without_case_bypass(self):
        orchestrator = (ROOT / 'validation' / 'real_game' / 'run_windows_validation.ps1').read_text(encoding='utf-8')
        live = (ROOT / 'validation' / 'real_game' / 'run_live_check.ps1').read_text(encoding='utf-8')
        fixture = (ROOT / 'validation' / 'real_game' / 'renpy' / 'game' / 'script.rpy').read_text(encoding='utf-8')
        self.assertNotIn('case_matrix', orchestrator)
        self.assertNotIn('NativeCase', live)
        self.assertNotIn('AUTOADV_VALIDATION_CASE', fixture)
        self.assertIn("'native' { 16 }", live)
        self.assertIn('jump continuation', fixture)
        self.assertNotIn('autoadv_case_', fixture)

    def test_hosted_and_broad_ci_do_not_run_real_audio(self):
        live = (ROOT / '.github' / 'workflows' / 'live-advancement.yml').read_text(encoding='utf-8')
        self.assertIn('-Phase reading', live)
        for forbidden in ('-Phase native', 'prepare_asr_model', 'AutoAdv.ProcessLoopback.csproj'):
            self.assertNotIn(forbidden, live)

        for name in ('ci.yml', 'windows-adapters.yml'):
            source = (ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8')
            self.assertNotIn('validation.real_game.test_native_audio', source)
            self.assertNotIn('run_live_check.ps1', source)


if __name__ == '__main__':
    unittest.main()
