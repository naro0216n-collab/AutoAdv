from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import urllib.error

from validation.real_game.fixture_runtime import (
    FIXTURE_RELEASE, acquire_archive, asset_name, source_digest, verify_runtime, write_manifest,
)


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


    def test_fixture_asset_name_and_missing_asset_wait_are_centralized(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'script.rpy').write_text('label start:\n    return\n', encoding='utf-8')
            expected = f'autoadv-validation-game-{source_digest(source)}.zip'
            self.assertEqual(asset_name(source=source), expected)

            now = [0.0]
            calls = []

            def clock():
                return now[0]

            def sleep(seconds):
                now[0] += seconds

            def downloader(url, destination):
                calls.append(url)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
                destination.write_bytes(b'fixture-zip')

            archive = acquire_archive(
                root / 'cache', source=source, wait_seconds=20,
                clock=clock, sleep=sleep, downloader=downloader)
            self.assertEqual(archive.name, expected)
            self.assertEqual(archive.read_bytes(), b'fixture-zip')
            self.assertEqual(len(calls), 2)
            self.assertIn(f'/releases/download/{FIXTURE_RELEASE}/{expected}', calls[0])

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
        done = source.index('elif event == "end"', begin)
        begin_block = source[begin:done]
        self.assertNotIn('_adv_oracle_voice = None', begin_block)
        self.assertNotIn('_adv_oracle_last_active = None', begin_block)
        self.assertNotIn('line_dismissed', source)


    def test_gateway_dispatches_fixture_build_and_local_runner_has_no_release_registration_copy(self):
        gateway = Path('.github/workflows/publish-gateway.yml').read_text(encoding='utf-8')
        fixture = Path('.github/workflows/validation-fixture.yml').read_text(encoding='utf-8')
        live = Path('.github/workflows/live-advancement.yml').read_text(encoding='utf-8')
        local = Path('validation/real_game/run_windows_validation.ps1').read_text(encoding='utf-8')
        runner = Path('validation/real_game/run_live_check.ps1').read_text(encoding='utf-8')
        for consumer in (local, live):
            self.assertIn('validation.real_game.fixture_runtime stage', consumer)
            self.assertNotIn('Invoke-WebRequest', consumer)
            self.assertNotIn('gh release download', consumer)
            self.assertNotIn('autoadv-validation-fixtures', consumer)
        self.assertIn('validation-fixture.yml/dispatches', gateway)
        self.assertIn('validation/real_game/renpy validation/real_game/fixture_runtime.py', gateway)
        self.assertIn('workflow_dispatch:', fixture)
        self.assertNotIn('pull_request:', fixture)
        self.assertNotIn('[BUILD AUTOADV FIXTURE]', fixture)
        self.assertIn('gh release upload', fixture)
        self.assertIn('renpy.org', fixture)
        self.assertIn('launcher distribute', fixture)
        self.assertNotIn('RenPySdk', runner)
        self.assertNotIn('dotnet build', runner)
        self.assertNotIn('prepare_asr_model', runner)


    def test_accuracy_contract_is_not_a_broad_fixture_discovery_command(self):
        readme = Path('validation/real_game/README.md').read_text(encoding='utf-8')
        self.assertIn('python -m unittest validation.real_game.test_native_audio -v', readme)
        self.assertIn('一括 `unittest discover` を自動進行精度実機計測として使用しない', readme)

    def test_hosted_live_excludes_accuracy_measurement_and_actual_machine_path_owns_it(self):
        live = Path('.github/workflows/live-advancement.yml').read_text(encoding='utf-8')
        launcher = Path('validation/real_game/run_windows_validation.ps1').read_text(encoding='utf-8')
        readme = Path('validation/real_game/README.md').read_text(encoding='utf-8')
        self.assertIn('-Phase reading', live)
        self.assertNotIn('-Phase accuracy', live)
        self.assertNotIn('prepare_accuracy_assets', live)
        self.assertNotIn('AutoAdv.ProcessLoopback.csproj', live)
        self.assertIn('scripts\\prepare_windows_runtime.py', launcher)
        self.assertIn('validation.real_game.prepare_accuracy_assets', launcher)
        self.assertNotIn('AutoAdv.ProcessLoopback.csproj', launcher)
        self.assertIn('Windows実機の自動進行精度計測', readme)
        self.assertIn('一括／Fullテストへ含めない', readme)


if __name__ == '__main__':
    unittest.main()
