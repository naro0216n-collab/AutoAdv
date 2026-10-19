import json
import unittest
from autoadv.luna_bridge import decode
from scripts.luna_bridge_patch import patch_text


class LunaBridgeTests(unittest.TestCase):
    def test_candidate_keeps_symbol_and_empty(self):
        for text in ("", "……", "おはよう", "おはよう"):
            p=dict(schema_version=1,source_seq=1,observed_at_mono_ms=100,
                raw_text=text,source="HOOK",event_kind="line_candidate")
            cand=decode(json.dumps(p).encode())
            self.assertEqual(cand.raw_text,text)
            self.assertFalse(cand.text_ready)
            self.assertFalse(cand.line_identity_verified)

    def test_reject_unexpected_line_id_or_ready_flag(self):
        p=dict(schema_version=1,source_seq=1,observed_at_mono_ms=100,
               raw_text="a",source="HOOK",event_kind="line_candidate",text_ready=True)
        with self.assertRaises(ValueError):decode(json.dumps(p).encode())

    def test_patch_at_prefilter_and_reversible(self):
        original='''    def textgetmethod_1(\n        self,\n        text,\n        isRefresh=False,\n    ):\n        if not text:\n            return\n'''
        updated=patch_text(original)
        self.assertLess(updated.index('AUTOADV_LUNA_BRIDGE_V1_BEGIN'),updated.index('if not text:'))
        self.assertEqual(patch_text(updated,undo=True),original)
        with self.assertRaises(ValueError):patch_text(updated)

if __name__=="__main__":unittest.main()