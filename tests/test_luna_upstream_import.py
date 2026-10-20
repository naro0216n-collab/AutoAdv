import importlib.util
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

class UpstreamImportTests(TestCase):
    def test_snapshot_is_commit_pinned_and_repeatable(self):
        spec=importlib.util.spec_from_file_location('sync_luna_upstream',Path(__file__).parents[1]/'scripts'/'sync_luna_upstream.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            repo=root/'upstream';repo.mkdir()
            subprocess.check_call(['git','init','--quiet',str(repo)])
            subprocess.check_call(['git','-C',str(repo),'config','user.name','Test'])
            subprocess.check_call(['git','-C',str(repo),'config','user.email','test@example.invalid'])
            for path in module.PATHS:
                if path.endswith('/'):
                    target=repo/path/'marker.cpp'
                else:target=repo/path
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text('source '+path,encoding='utf-8')
            subprocess.check_call(['git','-C',str(repo),'add','.'])
            subprocess.check_call(['git','-C',str(repo),'commit','--quiet','-m','upstream'])
            rev=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
            folder=root/'vendor'/'luna'
            plan=module.adopt(folder,revision=rev,local_upstream=repo)
            self.assertFalse(folder.exists())
            self.assertEqual(plan['files'],len(module.PATHS))
            committed=module.adopt(folder,revision=rev,local_upstream=repo,write=True)
            self.assertTrue((folder/'manifest.json').exists())
            self.assertEqual(committed['added'],plan['added'])
            self.assertEqual(module.adopt(folder,revision=rev,local_upstream=repo)['changed'],[])
            self.assertEqual(module.adopt(folder,revision=rev,local_upstream=repo)['added'],[])
