"""Native injection patch must not rewrite adopted upstream source."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from scripts.prepare_luna_native import prepare,HOST_CMAKE,HOST_DLL

class NativeBuildCopyContracts(TestCase):
    def test_enables_existing_upstream_injection_in_separate_copy(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'vendor'
            for item in (HOST_CMAKE,HOST_DLL):
                (source/item).parent.mkdir(parents=True,exist_ok=True)
            cmake='add_library(host host.cpp textthread.cpp ../include/InfoStrings.cpp)\n'
            dll='#if 0\nC_LUNA_API void Luna_ConnectAndInjectProcess(DWORD pid)\n{\n}\n#endif\nC_LUNA_API bool Luna_CheckIfNeedInject'
            (source/HOST_CMAKE).write_text(cmake)
            (source/HOST_DLL).write_text(dll)
            checks={str(item):hashlib.sha256((source/item).read_bytes()).hexdigest()
                    for item in (HOST_CMAKE,HOST_DLL)}
            (source/'manifest.json').write_text(json.dumps({'revision':'51884b6c88cba8c42e98ce3e43c4f408017082a1','sha256':checks}))
            target=root/'build-source'
            prepare(source,target)
            self.assertEqual((source/HOST_CMAKE).read_text(),cmake)
            self.assertEqual((source/HOST_DLL).read_text(),dll)
            self.assertIn('host.cpp injector.cpp', (target/HOST_CMAKE).read_text())
            self.assertNotIn('#if 0', (target/HOST_DLL).read_text())
            with self.assertRaises(FileExistsError):prepare(source,target)
            (source/HOST_DLL).write_text('changed')
            with self.assertRaises(ValueError):prepare(source,root/'another-build')
