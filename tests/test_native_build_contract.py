"""The prepared runtime is patched, the pinned upstream files remain pristine."""
from __future__ import annotations
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_luna_native import HOST_CMAKE, HOST_DLL, prepare


class NativeBuildContract(unittest.TestCase):
    def test_prepared_native_source_enables_injection_without_mutating_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'vendor'
            source.mkdir()
            dll = source / HOST_DLL
            cmake = source / HOST_CMAKE
            for path in (dll, cmake):
                path.parent.mkdir(parents=True, exist_ok=True)
            original_dll = '#if 0\nC_LUNA_API void Luna_ConnectAndInjectProcess() {}\n#endif\nC_LUNA_API bool Luna_CheckIfNeedInject'
            original_cmake = 'add_library(host host.cpp textthread.cpp ../include/InfoStrings.cpp)'
            dll.write_text(original_dll, encoding='utf-8')
            cmake.write_text(original_cmake, encoding='utf-8')
            manifest = {'revision': '51884b6c88cba8c42e98ce3e43c4f408017082a1',
                'sha256': {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (dll, cmake)}}
            (source / 'manifest.json').write_text(json.dumps(manifest))
            prepared = root / 'built'
            prepare(source, prepared)
            self.assertIn('C_LUNA_API void Luna_ConnectAndInjectProcess',
                          (prepared / HOST_DLL).read_text())
            self.assertNotIn('#if 0', (prepared / HOST_DLL).read_text())
            self.assertIn('injector.cpp', (prepared / HOST_CMAKE).read_text())
            self.assertEqual(dll.read_text(), original_dll)
            self.assertEqual(cmake.read_text(), original_cmake)
            patch_info = json.loads((prepared / 'NATIVE_PATCH.json').read_text(encoding='utf-8'))
            self.assertEqual(patch_info['patched_paths'], [HOST_DLL.as_posix(), HOST_CMAKE.as_posix()])
            with self.assertRaises(FileExistsError):
                prepare(source, prepared)
            dll.write_text('modified')
            with self.assertRaises(ValueError):
                prepare(source, root / 'tampered')
