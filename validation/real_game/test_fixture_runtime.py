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
            (runtime / 'game').mkdir()
            (runtime / 'game' / 'archive.rpa').write_bytes(b'fixture-data')
            manifest = write_manifest(runtime, exe, source=source,
                                      renpy_version='8.4.1', sdk_sha256='ab' * 32)
            self.assertEqual(verify_runtime(runtime, source=source), exe)
            self.assertEqual(manifest['executable'], 'AutoAdvValidationGame.exe')
            (runtime / 'game' / 'archive.rpa').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'runtime hash mismatch'):
                verify_runtime(runtime, source=source)

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



if __name__ == '__main__':
    unittest.main()
