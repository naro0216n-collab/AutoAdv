import tempfile
from pathlib import Path
import unittest
try:
    import numpy as np
    import cv2
except ImportError:
    np = cv2 = None
from autoadv.screen import ScreenObserver, ScreenProfile, Marker, recognize_markers
from autoadv.contracts import ScreenState

@unittest.skipUnless(cv2 is not None and np is not None, "requires the visual runtime extras")
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

class SymbolicAndEmptyTests(unittest.TestCase):
    def test_visible_key_keeps_symbols_where_asr_key_drops_them(self):
        from autoadv.screen import visible_key
        from autoadv.audio_match import phonetic_key
        self.assertEqual(visible_key(' …… '),visible_key('……'))
        self.assertNotEqual(visible_key('……'),visible_key('！！'))
        self.assertEqual(phonetic_key('……'),'')

    @unittest.skipUnless(cv2 is not None and np is not None, "requires the visual runtime extras")
    def test_optional_empty_positive_marker(self):
        import cv2,numpy as np
        from autoadv.screen import ScreenObserver,ScreenProfile,Marker
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            keys=['dialogue','text_ready','choice','map','menu','transition','empty_event']
            markers={}
            frame=np.zeros((12,50,3),dtype=np.uint8)
            for i,key in enumerate(keys):
                img=np.full((4,4,3),30+i*30,dtype=np.uint8)
                file=base/(key+'.png');cv2.imwrite(str(file),img)
                markers[key]=Marker(file,(i*5,0,4,4),.99)
                if key in ('dialogue','text_ready','empty_event'):
                    frame[0:4,i*5:i*5+4]=img
            o=ScreenObserver(ScreenProfile(markers,(0,6,30,4)),lambda:frame,ocr=lambda _: '')
            o.observe('',100)
            v=o.observe('',240)
            self.assertTrue(v.empty_event_confirmed)
            self.assertTrue(v.text_matches)
