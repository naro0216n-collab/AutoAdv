from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest
from unittest.mock import patch

from autoadv import native_runtime


class ManagedNativeRuntimeTests(unittest.TestCase):
    def test_managed_paths_are_stable_for_pinned_model_and_helper_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = native_runtime.managed_paths(runtime_root=root)
            self.assertEqual(Path(paths.asr_model).parent.name, native_runtime.ASR_REVISION)
            self.assertEqual(Path(paths.asr_model).name, 'model.int8.onnx')
            self.assertEqual(Path(paths.asr_tokens).name, 'tokens.txt')
            self.assertEqual(Path(paths.helper_dll).parent.name, paths.helper_digest)
            self.assertEqual(len(paths.helper_digest), 64)

    def test_existing_pinned_asr_assets_are_verified_without_download(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = native_runtime.managed_paths(runtime_root=root)
            directory = Path(paths.asr_model).parent
            directory.mkdir(parents=True)
            payloads = {'model.int8.onnx': b'model', 'tokens.txt': b'tokens'}
            expected = {name: native_runtime.hashlib.sha256(data).hexdigest()
                        for name, data in payloads.items()}
            with patch.object(native_runtime, 'ASR_FILES', expected):
                for name, data in payloads.items():
                    (directory / name).write_bytes(data)
                result = native_runtime.prepare_asr_model(runtime_root=root, allow_download=False)
            self.assertTrue(all(row['source'] == 'existing' for row in result['files']))

    def test_helper_build_uses_the_proven_project_build_and_managed_cache(self):
        repo = Path(__file__).resolve().parents[1]
        command = native_runtime._dotnet_build_command(repo)
        self.assertEqual(command[:2], ['dotnet', 'build'])
        self.assertIn('AutoAdv.ProcessLoopback.csproj', ' '.join(command))
        self.assertNotIn('--output', command)
        self.assertFalse(any(arg.startswith('-p:BaseIntermediateOutputPath=') for arg in command))

    def test_standard_windows_bootstrap_installs_runtime_dependencies_once(self):
        from scripts import prepare_windows_runtime as runtime
        repo = Path(__file__).resolve().parents[1]
        requirements = runtime.runtime_requirements(repo)
        self.assertIn('PySide6-Essentials>=6.8,<7', requirements)
        self.assertIn('sherpa-onnx>=1.11,<2', requirements)
        self.assertIn('haqumei>=0.12,<0.13', requirements)
        source = Path('scripts/prepare_windows_runtime.py').read_text(encoding='utf-8')
        self.assertIn('--only-binary=:all:', source)
        self.assertNotIn('.[gui,audio,ocr,native]', source)
        self.assertIn('.autoadv-dependencies.json', source)
        self.assertIn('autoadv.native_runtime', source)
        launcher = Path('AutoAdv.cmd').read_text(encoding='utf-8')
        self.assertIn('prepare_windows_runtime.py', launcher)
        self.assertIn('--launch', launcher)
        self.assertNotIn('pip install', launcher)


if __name__ == '__main__':
    unittest.main()
