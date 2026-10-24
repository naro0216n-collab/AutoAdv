from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoadv import native_runtime


class ManagedNativeRuntimeTests(unittest.TestCase):
    def test_managed_paths_are_stable_for_pinned_alignment_model_and_helper_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = native_runtime.managed_paths(runtime_root=root)
            self.assertEqual(Path(paths.alignment_model).parent.name, native_runtime.ALIGNMENT_REVISION)
            self.assertEqual(Path(paths.alignment_model).name, 'model.q4.onnx')
            self.assertEqual(Path(paths.alignment_vocab).name, 'vocab.json')
            self.assertEqual(Path(paths.helper_dll).parent.name, paths.helper_digest)
            self.assertEqual(len(paths.helper_digest), 64)
            self.assertEqual(native_runtime.ALIGNMENT_REPOSITORY,
                             'air-air/japanese-hubert-base-phoneme-ctc-v4-ONNX')
            self.assertEqual(native_runtime.ALIGNMENT_REMOTE_MODEL, 'onnx/model_q4.onnx')
            self.assertEqual(native_runtime.ALIGNMENT_MODEL_BYTES, 63_010_793)
            self.assertEqual(native_runtime.ALIGNMENT_VOCAB['ty'], 42)
            self.assertEqual(max(native_runtime.ALIGNMENT_VOCAB.values()) + 1, 48)

    def test_existing_pinned_alignment_asset_is_verified_without_download(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = native_runtime.managed_paths(runtime_root=root)
            model = Path(paths.alignment_model)
            model.parent.mkdir(parents=True)
            payload = b'model'
            model.write_bytes(payload)
            expected = native_runtime.hashlib.sha256(payload).hexdigest()
            with (patch.object(native_runtime, 'ALIGNMENT_MODEL_SHA256', expected),
                  patch.object(native_runtime, 'ALIGNMENT_MODEL_BYTES', len(payload))):
                result = native_runtime.prepare_alignment_model(
                    runtime_root=root, allow_download=False)
            self.assertEqual(result['model']['source'], 'existing')
            self.assertEqual(result['license'], 'Apache-2.0')
            self.assertTrue(Path(result['notice']).is_file())
            vocab = Path(paths.alignment_vocab)
            self.assertTrue(vocab.is_file())
            self.assertEqual(native_runtime.json.loads(vocab.read_text()), native_runtime.ALIGNMENT_VOCAB)

    def test_helper_build_uses_the_proven_project_build_and_managed_cache(self):
        repo = Path(__file__).resolve().parents[1]
        command = native_runtime._dotnet_build_command(repo)
        self.assertEqual(command[:2], ['dotnet', 'build'])
        self.assertIn('AutoAdv.ProcessLoopback.csproj', ' '.join(command))
        self.assertNotIn('--output', command)
        self.assertFalse(any(arg.startswith('-p:BaseIntermediateOutputPath=') for arg in command))

    def test_standard_windows_bootstrap_installs_alignment_runtime_not_free_asr_stack(self):
        from scripts import prepare_windows_runtime as runtime
        repo = Path(__file__).resolve().parents[1]
        requirements = runtime.runtime_requirements(repo)
        self.assertIn('PySide6-Essentials>=6.8,<7', requirements)
        self.assertIn('onnxruntime>=1.20,<2', requirements)
        self.assertIn('haqumei>=0.12,<0.13', requirements)
        self.assertFalse(any('sherpa' in requirement for requirement in requirements))
        self.assertFalse(any('webrtcvad' in requirement for requirement in requirements))
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
