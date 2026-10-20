"""Contracts for the adopted upstream runtime, not an alternative injector."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from autoadv import luna_runtime
from autoadv.model import Profile
from autoadv.storage import Catalog, save, load
from scripts import adopt_luna_release

ROOT = Path(__file__).resolve().parents[1]


class LunaVendorContracts(unittest.TestCase):
    def test_source_snapshot_is_committed_and_coherent(self):
        root = ROOT / 'vendor' / 'luna'
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['revision'], luna_runtime.COMMIT)
        self.assertGreater(manifest['file_count'], 900)
        for name in ('src/NativeImpl/LunaHook/LunaHost/LunaHostDll.cpp',
                     'src/LunaTranslator/textio/textsource/texthook.py',
                     'src/LunaTranslator/myutils/post.py', 'LICENSE'):
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(),
                             manifest['sha256'][name])

    def test_per_game_luna_capture_configuration_is_persisted(self):
        cat = Catalog()
        game = cat.create_game('game', 'C:/game.exe')
        p = game.profiles[game.default_profile]
        p.hook_flush_ms = 850
        p.hook_codepage = 932
        p.hook_max_buffer = 7500
        p.hook_max_history = 500000
        p.validate()
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / 'settings.json'
            save(cat, settings)
            restored = load(settings).games[game.id].profiles[p.id]
            self.assertEqual((restored.hook_flush_ms, restored.hook_codepage,
                              restored.hook_max_buffer, restored.hook_max_history),
                             (850, 932, 7500, 500000))
            restored.hook_flush_ms = -1
            with self.assertRaises(ValueError):
                restored.validate()

    def test_release_adoption_validates_integrity_before_any_install(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'luna_assets'
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as z:
                for name in luna_runtime.FILES:
                    z.writestr('files/' + name, b'dummy-' + name.encode('ascii'))
            with self.assertRaises(ValueError):
                adopt_luna_release.adopt(archive.getvalue(), destination)
            self.assertFalse(destination.exists())
            expected = hashlib.sha256(archive.getvalue()).hexdigest()
            with patch.object(adopt_luna_release, 'ZIP_SHA256', expected):
                manifest = adopt_luna_release.adopt(archive.getvalue(), destination)
                runtime = luna_runtime.LunaRuntime(destination)
                runtime.verify()
                self.assertEqual(manifest['release'], luna_runtime.RELEASE)
                self.assertEqual(manifest['commit'], luna_runtime.COMMIT)
                with self.assertRaises(FileExistsError):
                    adopt_luna_release.adopt(archive.getvalue(), destination)
                (destination / 'LunaHost64.dll').write_bytes(b'mutated')
                with self.assertRaises(ValueError):
                    runtime.verify()

    def test_upstream_injection_uses_matching_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = luna_runtime.LunaRuntime(root)
            class Completed:
                returncode = 1
            with patch.object(luna_runtime, 'is_process_64bit', return_value=False), \
                 patch('subprocess.run', return_value=Completed()) as subprocess_run:
                luna_runtime.inject(123, runtime)
                args = subprocess_run.call_args.args[0]
                self.assertEqual(args[0], str(root / 'LunaSubProcess32.exe'))
                self.assertEqual(args[1:3], ['dllinject', '123'])
                self.assertEqual(args[3], str(root / 'LunaHook32.dll'))
            class Failed:
                returncode = 0
            with patch.object(luna_runtime, 'is_process_64bit', return_value=True), \
                 patch('subprocess.run', return_value=Failed()):
                with self.assertRaisesRegex(RuntimeError, 'injection failed'):
                    luna_runtime.inject(123, runtime)


if __name__ == '__main__':
    unittest.main()
