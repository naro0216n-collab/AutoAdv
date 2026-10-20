"""Real image capture/probe validation for an opt-in, game-specific input provider."""
import json
import tempfile
import unittest
from pathlib import Path
from autoadv.screen_calibration import validate_roi,save_calibration,probe,immutable_calibration_path
from autoadv.contracts import ScreenState
from autoadv.screen import ScreenObserver,ScreenProfile


class VisualCalibrationContracts(unittest.TestCase):
    def test_capture_probe_and_image_change_do_not_invent_permission(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            frame=np.zeros((100,140,3),dtype='uint8')
            frame[15:30,10:30]=180
            rect=[10,15,20,15]
            marker=save_calibration(frame,rect,Path(d)/'advance.png',role='advance_allowed')
            document={'markers':{'advance_allowed':{'image':str(marker),'rect':rect,'min_score':.95}}}
            self.assertEqual(probe(frame,document)[:2],(ScreenState.ADVANCE_INDICATOR,True))
            frame[15:30,10:30]=0
            state,ready,_,scores=probe(frame,document)
            self.assertEqual(state,ScreenState.UNKNOWN)
            self.assertFalse(ready)
            self.assertLess(scores['advance_allowed'],.95)

    def test_invalid_roi_and_unrecognized_role_never_save(self):
        import numpy as np
        frame=np.zeros((100,100,3),dtype='uint8')
        for rect in ((0,0,0,10),(95,0,10,10),(-1,0,10,10)):
            with self.subTest(rect=rect),self.assertRaises(ValueError):validate_roi(frame,rect)
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'unused.png'
            with self.assertRaises(ValueError):save_calibration(frame,(0,0,10,10),target,role='dialogue')
            self.assertFalse(target.exists())

    def test_change_of_client_geometry_invalidates_all_permission_and_templates_remain_immutable(self):
        import numpy as np
        import cv2
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            frame=np.zeros((60,80,3),dtype='uint8')
            frame[0:10,0:10]=180
            old=immutable_calibration_path(frame,(0,0,10,10),root,role='advance_allowed')
            save_calibration(frame,(0,0,10,10),old,role='advance_allowed')
            data={'client_size':[80,60],'minimum_ready_span_ms':100,
                  'markers':{'advance_allowed':{'image':str(old),'rect':[0,0,10,10],
                                                 'source_client_size':[80,60]}}}
            config=root/'screen.json';config.write_text(json.dumps(data),encoding='utf-8')
            images=[frame]
            observer=ScreenObserver(ScreenProfile.load(config),lambda:images[0])
            self.assertFalse(observer.observe(100).ready)
            self.assertTrue(observer.observe(210).ready)
            images[0]=np.pad(frame,((0,5),(0,5),(0,0)))
            unknown=observer.observe(350)
            self.assertEqual(unknown.state,ScreenState.UNKNOWN)
            self.assertFalse(unknown.positive)
            self.assertFalse(unknown.ready)
            self.assertIn('recalibration',unknown.evidence)
            data['markers']['advance_allowed']['source_client_size']=[85,65]
            config.write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaises(ValueError):ScreenProfile.load(config)
            modified=frame.copy();modified[0:10,0:10]=35
            candidate=immutable_calibration_path(modified,(0,0,10,10),root,role='advance_allowed')
            self.assertNotEqual(old,candidate)
            save_calibration(modified,(0,0,10,10),candidate,role='advance_allowed')
            self.assertEqual(int(cv2.imread(str(old))[0,0,0]),180)
