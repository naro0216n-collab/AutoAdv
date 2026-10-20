"""Acquisition state ownership and OCR as opt-in observation only."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch,Mock
import json
from autoadv.acquisition import SourceSelection,SourceKind,AcquisitionSession,TextCandidate
from autoadv.ocr import TesseractOCR,OCRWorker
from autoadv.profiles import GameCatalog,GameProfile
from autoadv.preferences import Preferences

class AcquisitionContracts(unittest.TestCase):
    def test_default_hook_does_not_import_or_require_ocr(self):
        self.assertEqual(GameProfile('p','standard').source().kind,SourceKind.HOOK)
        with TemporaryDirectory() as td:
            p=Path(td)/'settings.json'
            catalog=GameCatalog();catalog.create_game('demo')
            Preferences(catalog=catalog).save(p)
            self.assertEqual(Preferences.load(p).catalog.active()[1].source().kind,SourceKind.HOOK)

    def test_explicit_ocr_validates_parameters(self):
        value=SourceSelection(SourceKind.OCR,'tesseract','jpn',(3,4,100,45))
        self.assertEqual(SourceSelection.from_dict(value.to_dict()),value)
        for bad in [(0,0,0,20),(-1,0,20,20),(0,0,20,-2),None]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                SourceSelection(SourceKind.OCR,'tesseract','jpn',bad)
        with self.assertRaises(ValueError):SourceSelection.from_dict({'kind':'invalid'})

    def test_profile_switch_and_ocr_configuration_share_one_store(self):
        from autoadv.services import AutoAdvService
        with TemporaryDirectory() as td:
            settings=Path(td)/'settings.json'
            service=AutoAdvService(settings_file=settings)
            game=service.new_game('demo')
            selection=SourceSelection(SourceKind.OCR,'tesseract','jpn',(3,4,200,55))
            service.configure_acquisition(selection)
            self.assertFalse(service.autoplay_allowed)
            duplicate=service.profile_duplicate('OCR alternative')
            self.assertEqual(duplicate.source(),selection)
            service.configure_acquisition(SourceSelection())
            self.assertEqual(service.active()[1].source().kind,SourceKind.HOOK)
            persisted=Preferences.load(settings)
            self.assertEqual(persisted.catalog.games[game.game_id].profiles[game.default_profile_id].source(),selection)
            self.assertEqual(persisted.catalog.games[game.game_id].profiles[duplicate.profile_id].source().kind,SourceKind.HOOK)
            self.assertFalse('authorized' in json.dumps(persisted.catalog.to_dict()))

    def test_no_cross_generation_or_cross_game_leak(self):
        session=AcquisitionSession('game-A','profile-A',SourceSelection())
        e=TextCandidate('game-A','profile-A',SourceKind.HOOK,1,1,100,'本文','pid:hook')
        self.assertTrue(session.accept(e))
        self.assertFalse(session.accept(e))
        self.assertFalse(session.accept(TextCandidate('game-A','profile-A',SourceKind.OCR,1,2,101,'OCR','hwnd:roi')))
        session.change('game-B','profile-A',SourceSelection(SourceKind.OCR,'tesseract','jpn',(0,0,10,10)))
        self.assertFalse(session.accept(TextCandidate('game-B','profile-A',SourceKind.OCR,1,2,102,'旧OCR','hwnd:roi')))
        self.assertFalse(session.accept(TextCandidate('game-A','profile-A',SourceKind.OCR,2,1,102,'別作品','hwnd:roi')))
        self.assertTrue(session.accept(TextCandidate('game-B','profile-A',SourceKind.OCR,2,1,103,'現在OCR','hwnd:roi')))

    def test_tesseract_receives_only_explicit_client_rectangle(self):
        import numpy as np
        frame=np.zeros((40,80,3),dtype=np.uint8)
        frame[5:15,10:30]=255
        selection=SourceSelection(SourceKind.OCR,'tesseract','jpn',(10,5,20,10))
        reader=TesseractOCR(selection,lambda:frame)
        called=[]
        def execute(args,**kwargs):
            called.append((args,kwargs))
            return Mock(returncode=0,stdout='日本語\n'.encode('utf-8'),stderr=b'')
        with patch('autoadv.ocr.subprocess.run',side_effect=execute):
            self.assertEqual(reader.recognize(),'日本語')
        self.assertEqual(called[0][0],['tesseract','stdin','stdout','-l','jpn'])
        self.assertTrue(called[0][1]['input'].startswith(b'\x89PNG'))
        self.assertFalse(called[0][1].get('shell',False))

    def test_ocr_worker_drops_empty_text_and_preserves_origin_and_generation(self):
        frames=['一行目','', '二行目']
        class Reader:
            def __init__(self):
                self.selection=SourceSelection(SourceKind.OCR,'tesseract','jpn',(10,20,40,30))
                self.capture=type('Capture',(),{'hwnd':456})()
            def recognize(self):return frames.pop(0)
        observations=[];blank=[];errors=[]
        worker=OCRWorker(reader=Reader(),game_id='g',profile_id='p',generation=7,
            on_candidate=observations.append,on_blank=lambda:blank.append(True),
            on_failure=errors.append,interval_ms=200)
        class StopAfter:
            def __init__(self):self.calls=0
            def is_set(self):return self.calls>=3
            def wait(self,duration):self.calls+=1
        worker.stop_event=StopAfter()
        worker._run()
        self.assertEqual([x.text for x in observations],['一行目','二行目'])
        self.assertEqual([x.sequence for x in observations],[1,2])
        self.assertEqual([x.source_generation for x in observations],[7,7])
        self.assertIn('hwnd=456',observations[0].origin)
        self.assertIn('(10, 20, 40, 30)',observations[0].origin)
        self.assertEqual(len(blank),1)
        self.assertEqual(errors,[])

    def test_ocr_worker_failure_does_not_emit_synthetic_dialogue(self):
        class Reader:
            def recognize(self):raise RuntimeError('unable to capture')
        observations=[];errors=[]
        worker=OCRWorker(reader=Reader(),game_id='g',profile_id='p',generation=3,
            on_candidate=observations.append,on_failure=errors.append)
        worker._run()
        self.assertEqual(observations,[])
        self.assertIn('unable to capture',errors[0])

    def test_out_of_bounds_and_ocr_failure_not_empty_dialogue(self):
        import numpy as np
        frame=np.zeros((10,10,3),dtype=np.uint8)
        with self.assertRaises(RuntimeError):
            TesseractOCR(SourceSelection(SourceKind.OCR,'tesseract','jpn',(5,5,10,10)),lambda:frame).recognize()
        reader=TesseractOCR(SourceSelection(SourceKind.OCR,'no-install','jpn',(0,0,5,5)),lambda:frame)
        with patch('autoadv.ocr.subprocess.run',side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(RuntimeError,'OCR engine'):
                reader.recognize()

if __name__ == '__main__': unittest.main()
