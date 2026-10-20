"""The optional visual provider may only report independently calibrated input evidence.

Text acquisition, OCR readiness and transition ACK are not visual permission.
"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoadv.contracts import ScreenState
from autoadv.progression import VisualGate
from autoadv.screen import Marker, ScreenObserver, ScreenProfile, recognize_markers

ROLES=('manual_block','transition','advance_allowed')


class Patch:
    shape=(4,4,3)
    def __init__(self,value):self.value=value
    def astype(self,unused):return self
    def __sub__(self,other):return Patch(self.value-other.value)
    def __abs__(self):return Patch(abs(self.value))


class Frame:
    shape=(4,12,3)
    def __init__(self,roles):self.roles=set(roles)
    def __getitem__(self,slices):
        return Patch(255 if ROLES[slices[1].start//4] in self.roles else 0)


class CalibratedEvidence(unittest.TestCase):
    def setUp(self):
        self.markers={name:Marker(Path(name),(i*4,0,4,4)) for i,name in enumerate(ROLES)}
        self.numpy=SimpleNamespace(mean=lambda p:p.value,abs=lambda p:abs(p))

    def test_only_verified_advance_marker_can_allow_and_conflicts_block(self):
        templates={key:(value,Patch(255)) for key,value in self.markers.items()}
        with patch.dict('sys.modules',{'numpy':self.numpy}):
            self.assertEqual(recognize_markers(Frame({}),templates)[0],ScreenState.UNKNOWN)
            self.assertEqual(recognize_markers(Frame({'transition'}),templates)[0],ScreenState.TRANSITION)
            self.assertEqual(recognize_markers(Frame({'advance_allowed'}),templates)[:2],
                             (ScreenState.ADVANCE_INDICATOR,True))
            self.assertEqual(recognize_markers(Frame({'advance_allowed','transition'}),templates)[0],
                             ScreenState.UNKNOWN)
            self.assertEqual(recognize_markers(Frame({'advance_allowed','manual_block'}),templates)[0],
                             ScreenState.MANUAL_REQUIRED)

    def test_permission_requires_stability_and_is_revoked_by_manual_or_unknown(self):
        roles={'advance_allowed'}
        profile=ScreenProfile(self.markers,minimum_ready_observations=2,minimum_ready_span_ms=100)
        cv2=SimpleNamespace(IMREAD_COLOR=1,imread=lambda *args:Patch(255))
        with patch.dict('sys.modules',{'cv2':cv2,'numpy':self.numpy}):
            gate=VisualGate(ScreenObserver(profile,lambda:Frame(roles)))
            self.assertFalse(gate.observe(100).may_advance)
            self.assertFalse(gate.observe(150).may_advance)
            self.assertTrue(gate.observe(210).may_advance)
            roles.add('manual_block')
            self.assertFalse(gate.observe(220).may_advance)
            roles.clear()
            self.assertFalse(gate.observe(230).may_advance)
            roles.add('advance_allowed')
            self.assertFalse(gate.observe(240).may_advance)
            self.assertTrue(gate.observe(360).may_advance)
