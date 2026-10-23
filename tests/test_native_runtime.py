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

    def test_helper_build_outputs_are_managed_outside_source_tree(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Path(__file__).resolve().parents[1]
            paths = native_runtime.managed_paths(repo_root=repo, runtime_root=root)
            command = native_runtime._dotnet_build_command(
                repo, Path(paths.helper_dll).parent / 'out', Path(paths.helper_dll).parent / 'obj')
            self.assertIn('--output', command)
            self.assertTrue(any(arg.startswith('-p:BaseIntermediateOutputPath=') for arg in command))
            self.assertNotIn('integrations/windows-loopback/bin', ' '.join(command))

    def test_standard_windows_bootstrap_installs_all_product_extras_once(self):
        source = Path('scripts/prepare_windows_runtime.py').read_text(encoding='utf-8')
        self.assertIn('.[gui,audio,ocr,native]', source)
        self.assertIn('.autoadv-dependencies.json', source)
        self.assertIn('autoadv.native_runtime', source)
        launcher = Path('AutoAdv.cmd').read_text(encoding='utf-8')
        self.assertIn('prepare_windows_runtime.py', launcher)
        self.assertIn('--launch', launcher)
        self.assertNotIn('pip install', launcher)


if __name__ == '__main__':
    unittest.main()
