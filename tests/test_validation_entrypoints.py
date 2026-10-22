from pathlib import Path
import unittest

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

    def test_cmd_is_only_a_develop_bootstrap(self):
        source = (ROOT / 'AutoAdv-Windows-Validate.cmd').read_text(encoding='utf-8')
        self.assertIn('--branch develop', source)
        self.assertIn('-WorkBase $base', source)
        self.assertIn('validation\\real_game\\run_windows_validation.ps1', source)
        for stale in ('RenPySdk', 'prepare_asr_model', 'AutoAdv.ProcessLoopback.csproj', 'run_live_check.ps1'):
            self.assertNotIn(stale, source)


if __name__ == '__main__':
    unittest.main()
