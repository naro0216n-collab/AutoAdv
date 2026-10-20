"""Positive screen-evidence contracts; unit tests do not require Windows, CV2 or NumPy."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from autoadv.screen import Marker,ScreenProfile,ScreenObserver,recognize_markers
from autoadv.contracts import ScreenState

ROLES=('dialogue','text_ready','manual_block','transition','advance_allowed')

class FakePatch:
    shape=(4,4,3)
    def __init__(self,value):self.value=value
    def astype(self,unused):return self
    def __sub__(self,other):return FakePatch(self.value-other.value)
    def __abs__(self):return FakePatch(abs(self.value))

class FakeFrame:
    def __init__(self,roles):self.roles=roles
    def __getitem__(self,slices):
        return FakePatch(255 if ROLES[slices[1].start//4] in self.roles else 0)

class ScreenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.templates={name:(Marker(Path(name),(i*4,0,4,4)),FakePatch(255))
                        for i,name in enumerate(ROLES)}
        self.numpy=SimpleNamespace(mean=lambda patch:patch.value,abs=lambda patch:abs(patch))
    def see(self,*roles):
        with patch.dict('sys.modules',{'numpy':self.numpy}):
            return recognize_markers(FakeFrame(set(roles)),self.templates)
    def test_requires_positive_dialogue(self):
        self.assertEqual(self.see()[0],ScreenState.UNKNOWN)
        self.assertEqual(self.see('dialogue')[:2],(ScreenState.DIALOGUE,False))
        self.assertEqual(self.see('dialogue','text_ready')[:2],(ScreenState.DIALOGUE,True))
    def test_manual_block_wins_and_transition_is_not_permission(self):
        self.assertEqual(self.see('dialogue','manual_block')[0],ScreenState.MANUAL_REQUIRED)
        self.assertEqual(self.see('transition')[0],ScreenState.TRANSITION)
        self.assertEqual(self.see('dialogue','transition')[0],ScreenState.UNKNOWN)
    def test_schema_no_text_ocr_or_six_special_screens(self):
        self.assertEqual(set(ScreenProfile({k:v[0] for k,v in self.templates.items()}).markers),set(ROLES))
        self.assertEqual(set(ScreenProfile({'dialogue':self.templates['dialogue'][0]}).markers),{'dialogue'})
        with self.assertRaises(ValueError):ScreenProfile({})
    def test_advance_permission_requires_distinct_explicit_indicator(self):
        from autoadv.progression import VisualGate
        from autoadv.screen import ScreenSample
        class Observer:
            def __init__(self):self.sample=None
            def observe(self,now_ms):return self.sample
        obs=Observer();gate=VisualGate(obs)
        obs.sample=ScreenSample(100,ScreenState.DIALOGUE,True,True,'dialogue + text readiness (not input permission)')
        self.assertFalse(gate.observe(100).may_advance)
        obs.sample=ScreenSample(150,ScreenState.DIALOGUE,True,True,'advance_allowed-marker')
        self.assertTrue(gate.observe(150).may_advance)
        obs.sample=ScreenSample(200,ScreenState.MANUAL_REQUIRED,False,False,'manual_block marker')
        self.assertFalse(gate.observe(200).may_advance)
        self.assertEqual(self.see('advance_allowed')[:2],(ScreenState.DIALOGUE,True))
        self.assertEqual(self.see('advance_allowed','manual_block')[0],ScreenState.MANUAL_REQUIRED)
        self.assertEqual(self.see('advance_allowed','transition')[0],ScreenState.UNKNOWN)

    def test_stable_readiness_requires_multiple_positive_observations(self):
        templates=self.templates
        profile=ScreenProfile({k:v[0] for k,v in templates.items()},2,100)
        cv2=SimpleNamespace(IMREAD_COLOR=1,imread=lambda filename,mode:FakePatch(255))
        with patch.dict('sys.modules',{'cv2':cv2,'numpy':self.numpy}):
            observer=ScreenObserver(profile,lambda:FakeFrame({'dialogue','text_ready'}))
            self.assertFalse(observer.observe(100).ready)
            self.assertFalse(observer.observe(150).ready)
            self.assertTrue(observer.observe(205).ready)
