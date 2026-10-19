import tempfile
from pathlib import Path
import unittest
import numpy as np
import cv2
from autoadv.screen import ScreenObserver, ScreenProfile, Marker, recognize_markers
from autoadv.contracts import ScreenState

class VisualContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.rois={k:(i*4,0,4,4) for i,k in enumerate(('dialogue','text_ready','choice','map','menu','transition'))}
        self.markers={}
        for i,(role,roi) in enumerate(self.rois.items()):
            path=self.base/(role+'.png')
            # all markers are distinct, no accidental collision.
            cv2.imwrite(str(path),np.full((4,4,3),30+i*30,dtype=np.uint8))
            self.markers[role]=Marker(path,roi,.99)
        self.frame=np.zeros((10,35,3),dtype=np.uint8)
        self.profile=ScreenProfile(self.markers,(0,5,30,4))

    def tearDown(self):self.tmp.cleanup()

    def make(self,*roles):
        frame=self.frame.copy()
        for role in roles:
            x,y,w,h=self.rois[role]
            frame[y:y+h,x:x+w]=cv2.imread(str(self.markers[role].image))
        return frame

    def test_unknown_without_positive_dialogue(self):
        templates={k:(m,cv2.imread(str(m.image))) for k,m in self.markers.items()}
        self.assertEqual(recognize_markers(self.make(),templates),(ScreenState.UNKNOWN,False))
        self.assertEqual(recognize_markers(self.make('dialogue','choice'),templates),(ScreenState.UNKNOWN,False))
        self.assertEqual(recognize_markers(self.make('choice'),templates),(ScreenState.CHOICE,False))

    def test_readiness_requires_marker_span_and_independent_ocr(self):
        f=self.make('dialogue','text_ready')
        s=ScreenObserver(self.profile,lambda:f,ocr=lambda _: 'おはよう')
        self.assertFalse(s.observe('おはよう',100).ready)
        self.assertFalse(s.observe('おはよう',200).ready)
        observed=s.observe('おはよう',240)
        self.assertTrue(observed.ready)
        self.assertTrue(observed.text_matches)
        self.assertFalse(s.observe('違う',400).text_matches)

    def test_blocker_resets_ready_streak(self):
        f=[self.make('dialogue','text_ready')]
        s=ScreenObserver(self.profile,lambda:f[0],ocr=lambda _:'本文')
        s.observe('本文',100)
        f[0]=self.make('map')
        self.assertEqual(s.observe('本文',500).state,ScreenState.MAP)
        f[0]=self.make('dialogue','text_ready')
        self.assertFalse(s.observe('本文',600).ready)

if __name__=='__main__':unittest.main()
