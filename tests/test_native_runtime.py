from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoadv import native_runtime


class ManagedNativeRuntimeTests(unittest.TestCase):

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






if __name__ == '__main__':
    unittest.main()
