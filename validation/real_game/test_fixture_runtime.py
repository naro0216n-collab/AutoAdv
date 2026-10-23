from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from validation.real_game.fixture_runtime import source_digest, verify_runtime, write_manifest


class ValidationFixtureRuntimeContracts(unittest.TestCase):
    def test_source_digest_ignores_generated_renpy_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / 'game'
            game.mkdir()
            (game / 'script.rpy').write_text('label start:\n    return\n', encoding='utf-8')
            first = source_digest(root)
            (game / 'script.rpyc').write_bytes(b'generated')
            (game / 'cache').mkdir()
            (game / 'cache' / 'bytecode.rpyb').write_bytes(b'generated-cache')
            self.assertEqual(first, source_digest(root))
            license_file = root / 'CREDITS.txt'
            license_file.write_text('credits', encoding='utf-8')
            with_credits = source_digest(root)
            self.assertNotEqual(first, with_credits)
            license_file.write_text('credits changed', encoding='utf-8')
            self.assertNotEqual(with_credits, source_digest(root))
            (game / 'script.rpy').write_text('label start:\n    "changed"\n', encoding='utf-8')
            self.assertNotEqual(first, source_digest(root))


    def test_source_digest_normalizes_windows_text_line_endings_but_not_audio(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / 'game'
            audio = game / 'audio'
            audio.mkdir(parents=True)
            script = game / 'script.rpy'
            credits = root / 'CREDITS.txt'
            voice = audio / 'voice.wav'
            script.write_bytes(b'label start:\n    return\n')
            credits.write_bytes(b'credits\nline2\n')
            voice.write_bytes(b'RIFF\r\nBINARY')
            lf = source_digest(root)
            script.write_bytes(b'label start:\r\n    return\r\n')
            credits.write_bytes(b'credits\r\nline2\r\n')
            self.assertEqual(lf, source_digest(root))
            voice.write_bytes(b'RIFF\nBINARY')
            self.assertNotEqual(lf, source_digest(root))

    def test_manifest_binds_ready_runtime_to_current_fixture_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            (source / 'game').mkdir(parents=True)
            (source / 'game' / 'script.rpy').write_text('label start:\n    return\n', encoding='utf-8')
            runtime = root / 'runtime'
            runtime.mkdir()
            exe = runtime / 'AutoAdvValidationGame.exe'
            exe.write_bytes(b'fixture-exe')
            manifest = write_manifest(runtime, exe, source=source,
                                      renpy_version='8.4.1', sdk_sha256='ab' * 32)
            self.assertEqual(verify_runtime(runtime, source=source), exe)
            self.assertEqual(manifest['executable'], 'AutoAdvValidationGame.exe')

    def test_manifest_rejects_fixture_source_drift(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            (source / 'game').mkdir(parents=True)
            script = source / 'game' / 'script.rpy'
            script.write_text('label start:\n    return\n', encoding='utf-8')
            runtime = root / 'runtime'
            runtime.mkdir()
            exe = runtime / 'AutoAdvValidationGame.exe'
            exe.write_bytes(b'fixture-exe')
            write_manifest(runtime, exe, source=source,
                           renpy_version='8.4.1', sdk_sha256='cd' * 32)
            script.write_text('label start:\n    "changed"\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'does not match current fixture source'):
                verify_runtime(runtime, source=source)


    def test_game_oracle_keeps_voice_lifecycle_owner_across_dialogue_boundary(self):
        source = Path('validation/real_game/renpy/game/autoadv_oracle.rpy').read_text(encoding='utf-8')
        self.assertIn('_adv_oracle_voice_line = _adv_oracle_current', source)
        self.assertIn('"voice_ended", line=_adv_oracle_voice_line', source)
        begin = source.index('if event == "begin":')
        done = source.index('elif event == "interact_done"', begin)
        begin_block = source[begin:done]
        self.assertNotIn('_adv_oracle_voice = None', begin_block)
        self.assertNotIn('_adv_oracle_last_active = None', begin_block)


    def test_live_workflow_consumes_prebuilt_fixture_and_keeps_construction_separate(self):
        live = Path('.github/workflows/live-advancement.yml').read_text(encoding='utf-8')
        fixture = Path('.github/workflows/validation-fixture.yml').read_text(encoding='utf-8')
        runner = Path('validation/real_game/run_live_check.ps1').read_text(encoding='utf-8')
        self.assertIn('fixture_runtime source-digest', live)
        self.assertIn('gh release download autoadv-validation-fixtures', live)
        self.assertIn('-GameExecutable $env:AUTOADV_VALIDATION_GAME', live)
        self.assertNotIn('renpy.org', live)
        self.assertNotIn('RenPySdk', live)
        self.assertNotIn('launcher distribute', live)
        self.assertIn('renpy.org', fixture)
        self.assertIn('launcher distribute', fixture)
        self.assertIn('gh release upload', fixture)
        self.assertNotIn('RenPySdk', runner)
        self.assertNotIn('dotnet build', runner)
        self.assertNotIn('prepare_asr_model', runner)

    def test_real_audio_contract_is_not_a_broad_fixture_discovery_command(self):
        readme = Path('validation/real_game/README.md').read_text(encoding='utf-8')
        self.assertIn('python -m unittest validation.real_game.test_native_audio -v', readme)
        self.assertIn('一括 `unittest discover` をRealAudio検証として使用しない', readme)

    def test_hosted_live_excludes_real_audio_and_actual_machine_path_owns_it(self):
        live = Path('.github/workflows/live-advancement.yml').read_text(encoding='utf-8')
        launcher = Path('validation/real_game/run_windows_validation.ps1').read_text(encoding='utf-8')
        readme = Path('validation/real_game/README.md').read_text(encoding='utf-8')
        self.assertIn('-Phase reading', live)
        self.assertNotIn('-Phase native', live)
        self.assertNotIn('prepare_asr_model', live)
        self.assertNotIn('AutoAdv.ProcessLoopback.csproj', live)
        self.assertIn('validation.real_game.prepare_asr_model', launcher)
        self.assertIn('AutoAdv.ProcessLoopback.csproj', launcher)
        self.assertIn('Windows実機RealAudio', readme)
        self.assertIn('一括／Fullテストへ含めない', readme)


if __name__ == '__main__':
    unittest.main()
