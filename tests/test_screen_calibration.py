"""End-to-end calibration image and matcher contracts, no Win32 mocking."""
import tempfile
import unittest
from pathlib import Path
from autoadv.screen_calibration import validate_roi,save_calibration,probe
from autoadv.contracts import ScreenState

class CalibrationContracts(unittest.TestCase):
    def test_capture_exact_client_pixels_and_probe_runtime_matcher(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            frame=np.zeros((100,140,3),dtype='uint8')
            frame[15:30,10:30]=180
            frame[60:75,80:100]=220
            rect1=[10,15,20,15];rect2=[80,60,20,15]
            a=save_calibration(frame,rect1,Path(d)/'a.png',role='dialogue')
            b=save_calibration(frame,rect2,Path(d)/'b.png',role='text_ready')
            doc={'markers':{'dialogue':{'image':str(a),'rect':rect1,'min_score':.95},
                            'text_ready':{'image':str(b),'rect':rect2,'min_score':.95}}}
            state,ready,_,scores=probe(frame,doc)
            self.assertEqual(state,ScreenState.DIALOGUE)
            self.assertTrue(ready)
            self.assertAlmostEqual(scores['dialogue'],1)
            frame[15:30,10:30]=0
            state,ready,_,scores=probe(frame,doc)
            self.assertEqual(state,ScreenState.UNKNOWN)
            self.assertFalse(ready)
            self.assertLess(scores['dialogue'],.95)

    def test_outside_roi_and_unknown_roles_rejected(self):
        import numpy as np
        frame=np.zeros((100,100,3),dtype='uint8')
        for rect in ((0,0,0,10),(95,0,10,10),(-1,0,10,10)):
            with self.assertRaises(ValueError):validate_roi(frame,rect)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):save_calibration(frame,(0,0,10,10),Path(d)/'a.png',role='generic_ok')
            self.assertEqual(list(Path(d).iterdir()),[])

class ClientGeometrySafety(unittest.TestCase):
    def test_capture_version_is_immutable_and_resolution_change_fails_closed(self):
        import numpy as np
        from autoadv.screen_calibration import immutable_calibration_path
        from autoadv.screen import ScreenProfile,ScreenObserver
        import json
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            frame=np.zeros((60,80,3),dtype='uint8')
            frame[0:10,0:10]=180
            frame[20:30,20:30]=210
            old=immutable_calibration_path(frame,(0,0,10,10),directory,role='dialogue')
            save_calibration(frame,(0,0,10,10),old,role='dialogue')
            ready=immutable_calibration_path(frame,(20,20,10,10),directory,role='text_ready')
            save_calibration(frame,(20,20,10,10),ready,role='text_ready')
            data={'client_size':[80,60], 'minimum_ready_span_ms':100,
                  'markers':{'dialogue':{'image':str(old),'rect':[0,0,10,10],
                                           'source_client_size':[80,60]},
                             'text_ready':{'image':str(ready),'rect':[20,20,10,10],
                                             'source_client_size':[80,60]}}}
            path=directory/'screen.json'
            path.write_text(json.dumps(data),encoding='utf-8')
            profile=ScreenProfile.load(path)
            size=[frame]
            observer=ScreenObserver(profile,lambda:size[0])
            self.assertFalse(observer.observe(100).ready)
            self.assertTrue(observer.observe(210).ready)
            size[0]=np.pad(frame,((0,5),(0,5),(0,0)))
            sample=observer.observe(350)
            self.assertEqual(sample.state,ScreenState.UNKNOWN)
            self.assertFalse(sample.positive)
            self.assertFalse(sample.ready)
            self.assertIn('recalibration',sample.evidence)
            self.assertFalse(observer.observe(400).ready)
            data['markers']['text_ready']['source_client_size']=[85,65]
            path.write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaises(ValueError):ScreenProfile.load(path)
            changed=frame.copy();changed[0:10,0:10]=35
            candidate=immutable_calibration_path(changed,(0,0,10,10),directory,role='dialogue')
            self.assertNotEqual(candidate,old)
            save_calibration(changed,(0,0,10,10),candidate,role='dialogue')
            self.assertTrue(old.is_file())
            import cv2
            self.assertEqual(int(cv2.imread(str(old))[0,0,0]),180)

    def test_probe_rejects_changed_client_size_without_false_match(self):
        import numpy as np
        frame=np.zeros((50,100,3),dtype='uint8')
        document={'client_size':[99,50],'markers':{}}
        state,ready,reason,scores=probe(frame,document)
        self.assertEqual(state,ScreenState.UNKNOWN)
        self.assertFalse(ready)
        self.assertIn('size',reason)
        self.assertEqual(scores,{})
