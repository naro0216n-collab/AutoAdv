from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
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
            self.assertEqual(Path(paths.helper_executable).parent.name, paths.helper_digest)
            self.assertEqual(Path(paths.helper_executable).name, 'AutoAdv.ProcessLoopback.exe')
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

    def test_helper_is_downloaded_as_verified_self_contained_runtime_asset(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Path(__file__).resolve().parents[1]
            paths = native_runtime.managed_paths(repo_root=repo, runtime_root=root)
            asset_name, manifest_name = native_runtime._helper_asset_names(paths.helper_digest)
            payload = b'MZ-self-contained-helper'
            manifest = {
                'schema': 'autoadv-process-loopback-runtime-v1',
                'source_digest': paths.helper_digest,
                'filename': asset_name,
                'sha256': hashlib.sha256(payload).hexdigest(),
                'size_bytes': len(payload),
            }
            downloads = {
                native_runtime._runtime_asset_url(manifest_name): json.dumps(manifest).encode(),
                native_runtime._runtime_asset_url(asset_name): payload,
            }
            with patch.object(native_runtime, '_download_bytes',
                              side_effect=lambda url, max_bytes: downloads[url]) as download:
                result = native_runtime.prepare_helper(
                    repo_root=repo, runtime_root=root, allow_download=True)
            helper = Path(paths.helper_executable)
            self.assertEqual(helper.read_bytes(), payload)
            self.assertEqual(result['source'], 'downloaded')
            self.assertEqual(download.call_count, 2)

            with patch.object(native_runtime, '_download_bytes',
                              side_effect=AssertionError('verified cache must be reused')):
                cached = native_runtime.prepare_helper(
                    repo_root=repo, runtime_root=root, allow_download=False)
            self.assertEqual(cached['source'], 'existing')

    def test_helper_missing_in_verify_only_mode_does_not_build_locally(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Path(__file__).resolve().parents[1]
            with self.assertRaises(FileNotFoundError):
                native_runtime.prepare_helper(
                    repo_root=repo, runtime_root=root, allow_download=False)
            source = Path('autoadv/native_runtime.py').read_text(encoding='utf-8')
            self.assertNotIn('dotnet build', source)
            self.assertNotIn('shutil.which("dotnet")', source)

    def test_standard_windows_bootstrap_installs_binary_product_dependencies_once(self):
        source = Path('scripts/prepare_windows_runtime.py').read_text(encoding='utf-8')
        self.assertIn('.[gui,audio,ocr,native]', source)
        self.assertIn('--only-binary=:all:', source)
        self.assertIn('.autoadv-dependencies.json', source)
        self.assertIn('autoadv.native_runtime', source)
        launcher = Path('AutoAdv.cmd').read_text(encoding='utf-8')
        self.assertIn('prepare_windows_runtime.py', launcher)
        self.assertIn('--launch', launcher)
        self.assertNotIn('pip install', launcher)


if __name__ == '__main__':
    unittest.main()
