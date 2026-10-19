import json
import unittest
from autoadv.game_metadata import VoiceMetadataInbox,decode_asset_report,signed_asset_packet
from autoadv.controller import Line
from autoadv.text import normalize

class GameMetadataContract(unittest.TestCase):
    KEY=b'local-game-adapter-example-secret'
    def payload(self,**changes):
        data=dict(schema_version=1,adapter_seq=1,session_id='s',epoch=2,token=5,
                  source_line_id='visual:1:10',assigned=False,asset_id=None,
                  proof='game_current_line_asset_lookup')
        data.update(changes)
        return signed_asset_packet(data,self.KEY)

    def line(self):
        return Line(2,5,normalize('本文'),'visual:1:10',100,10)

    def test_positive_absence_needs_matching_own_generation_and_explicit_proof(self):
        inbox=VoiceMetadataInbox(self.KEY)
        self.assertFalse(inbox.accept(self.payload(epoch=1), 's', self.line()))
        self.assertIsNone(inbox.for_line('s',self.line()))
        self.assertFalse(inbox.accept(self.payload(), 's', self.line())) # duplicated adapter sequence
        self.assertTrue(inbox.accept(self.payload(adapter_seq=2), 's', self.line()))
        self.assertFalse(inbox.for_line('s',self.line()).asset_assigned)
        self.assertIsNone(inbox.for_line('s',Line(3,6,normalize('本文'),'visual:1:10',101,11)))

    def test_missing_or_unverified_proof_is_not_absence(self):
        with self.assertRaises(ValueError):decode_asset_report(self.payload(proof='speaker_guess'),self.KEY)
        with self.assertRaises(ValueError):decode_asset_report(self.payload(assigned=True,asset_id=None),self.KEY)
        with self.assertRaises(ValueError):decode_asset_report(self.payload(assigned='false'),self.KEY)

    def test_rejects_untrusted_local_udp_sender(self):
        raw=self.payload()
        with self.assertRaises(ValueError):decode_asset_report(raw,b'different-from-trusted-identity')
        with self.assertRaises(ValueError):decode_asset_report(raw[:-3]+b'xyz',self.KEY)

    def test_present_and_asset_identity(self):
        inbox=VoiceMetadataInbox(self.KEY)
        self.assertTrue(inbox.accept(self.payload(assigned=True,asset_id='game-asset-A'),'s',self.line()))
        self.assertEqual(inbox.for_line('s',self.line()).asset_id,'game-asset-A')

if __name__=='__main__':unittest.main()
