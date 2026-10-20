"""Positive-only screen classification without OCR or old menu taxonomy."""
import unittest
import numpy as np
from autoadv.screen import Marker,ScreenProfile,ScreenObserver,recognize_markers,ScreenSample
from autoadv.contracts import ScreenState
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

class ScreenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.zeros((4,16,3),dtype='uint8')
        self.templates={}
        for i,role in enumerate(('dialogue','text_ready','manual_block','transition')):
            self.templates[role]=(Marker(Path(role),(i*4,0,4,4)),np.full((4,4,3),255,dtype='uint8'))
    def enable(self,*roles):
        img=self.frame.copy()
        for role in roles:
            m,t=self.templates[role];x,y,w,h=m.rect;img[y:y+h,x:x+w]=t
        return img
    def test_requires_positive_dialogue(self):
        self.assertEqual(recognize_markers(self.enable(),self.templates)[0],ScreenState.UNKNOWN)
        self.assertEqual(recognize_markers(self.enable('dialogue'),self.templates)[:2],(ScreenState.DIALOGUE,False))
        self.assertEqual(recognize_markers(self.enable('dialogue','text_ready'),self.templates)[:2],(ScreenState.DIALOGUE,True))
    def test_manual_block_wins_and_transition_requires_ack(self):
        self.assertEqual(recognize_markers(self.enable('dialogue','manual_block'),self.templates)[0],ScreenState.MANUAL_REQUIRED)
        self.assertEqual(recognize_markers(self.enable('transition'),self.templates)[0],ScreenState.TRANSITION)
        self.assertEqual(recognize_markers(self.enable('dialogue','transition'),self.templates)[0],ScreenState.UNKNOWN)
    def test_screen_profile_has_no_ocr_field_or_six_categories(self):
        profile=ScreenProfile({k:v[0] for k,v in self.templates.items()})
        self.assertEqual(set(profile.markers),set(self.templates))
        with self.assertRaises(ValueError):ScreenProfile({'dialogue':self.templates['dialogue'][0]})
    def test_readiness_is_observed_across_time_not_one_frame(self):
        with TemporaryDirectory() as td:
            from pathlib import Path
            import cv2
            mapping={}
            for name,(m,t) in self.templates.items():
                f=Path(td)/(name+'.png');cv2.imwrite(str(f),t)
                mapping[name]=Marker(f,m.rect)
            observer=ScreenObserver(ScreenProfile(mapping,2,100),lambda:self.enable('dialogue','text_ready'))
            self.assertFalse(observer.observe(100).ready)
            self.assertFalse(observer.observe(150).ready)
            self.assertTrue(observer.observe(205).ready)
