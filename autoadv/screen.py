"""Positive visual permission, manual blocker and transition observations.

Luna supplies text. This adapter never OCRs text or classifies unnecessary
screen types. A missing marker is UNKNOWN, never permission.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import json
from .contracts import ScreenState

ROLES = frozenset(('dialogue','text_ready','manual_block','transition'))


@dataclass(frozen=True)
class Marker:
    image:Path
    rect:tuple[int,int,int,int]
    min_score:float=.96

    def __post_init__(self):
        x,y,w,h=self.rect
        if min(x,y)<0 or w<=0 or h<=0 or not .5<=self.min_score<=1:
            raise ValueError('invalid marker ROI or threshold')


@dataclass(frozen=True)
class ScreenProfile:
    markers:dict[str,Marker]
    minimum_ready_observations:int=2
    minimum_ready_span_ms:int=120

    def __post_init__(self):
        if not {'dialogue','text_ready'}.issubset(self.markers) or not set(self.markers).issubset(ROLES):
            raise ValueError('dialogue and readiness markers are required')
        if self.minimum_ready_observations<2 or self.minimum_ready_span_ms<=0:
            raise ValueError('invalid ready calibration')

    @classmethod
    def load(cls,path:Path) -> 'ScreenProfile':
        data=json.loads(path.read_text(encoding='utf-8'))
        if set(data)-{'markers','minimum_ready_observations','minimum_ready_span_ms'}:
            raise ValueError('unknown screen profile fields')
        return cls({name:Marker((path.parent / d['image']).resolve(),tuple(d['rect']),d.get('min_score',.96))
                    for name,d in data['markers'].items()},
                   data.get('minimum_ready_observations',2),data.get('minimum_ready_span_ms',120))


@dataclass(frozen=True)
class ScreenSample:
    at_ms:int
    state:ScreenState
    ready:bool
    positive:bool
    evidence:str


def recognize_markers(frame,templates:dict[str,tuple[Marker,object]]) -> tuple[ScreenState,bool,str]:
    import numpy as np
    matched=[]
    for role,(marker,template) in templates.items():
        x,y,w,h=marker.rect
        roi=frame[y:y+h,x:x+w]
        if roi.shape[:2]!=(h,w) or template.shape[:2]!=(h,w):
            return ScreenState.UNKNOWN,False,'invalid ROI'
        score=1-float(np.mean(np.abs(roi.astype('float32')-template.astype('float32'))))/255
        if score>=marker.min_score:matched.append(role)
    if 'manual_block' in matched:
        # Manual-required proof wins over any coincident dialogue indicator.
        return ScreenState.MANUAL_REQUIRED,False,'manual_block marker'
    if 'transition' in matched and 'dialogue' not in matched:
        return ScreenState.TRANSITION,False,'transition marker'
    if 'dialogue' not in matched or 'transition' in matched:
        return ScreenState.UNKNOWN,False,'no unambiguous dialogue marker'
    return ScreenState.DIALOGUE,'text_ready' in matched,'dialogue + ready markers'


class ScreenObserver:
    def __init__(self,profile:ScreenProfile,capture:Callable[[],object]):
        import cv2
        self.profile,self.capture=profile,capture
        self.templates={}
        for role,marker in profile.markers.items():
            arr=cv2.imread(str(marker.image),cv2.IMREAD_COLOR)
            if arr is None or arr.shape[:2]!=(marker.rect[3],marker.rect[2]):
                raise ValueError(f'calibration image missing/invalid for {role}')
            self.templates[role]=(marker,arr)
        self._ready_first_at=None
        self._ready_count=0

    def observe(self,at_ms:int)->ScreenSample:
        state,ready_marker,evidence=recognize_markers(self.capture(),self.templates)
        if state is ScreenState.DIALOGUE and ready_marker:
            if self._ready_first_at is None:self._ready_first_at=at_ms
            self._ready_count+=1
        else:
            self._ready_first_at=None
            self._ready_count=0
        stable=(self._ready_count>=self.profile.minimum_ready_observations and
                self._ready_first_at is not None and
                at_ms-self._ready_first_at>=self.profile.minimum_ready_span_ms)
        return ScreenSample(at_ms,state,stable,state is ScreenState.DIALOGUE,evidence)


class WindowsClientCapture:
    def __init__(self,hwnd:int,expected_pid:int):
        if hwnd<=0 or expected_pid<=0:raise ValueError('HWND and PID are required')
        self.hwnd,self.pid=hwnd,expected_pid

    def __call__(self):
        import sys
        if sys.platform!='win32':raise OSError('Windows screenshot capture required')
        import ctypes
        from ctypes import wintypes
        import numpy as np
        import mss
        user32=ctypes.windll.user32
        current_pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(self.hwnd),ctypes.byref(current_pid))
        if current_pid.value!=self.pid or not user32.IsWindowVisible(self.hwnd):
            raise RuntimeError('target HWND/PID/visibility changed')
        rect=wintypes.RECT()
        if not user32.GetClientRect(self.hwnd,ctypes.byref(rect)):raise OSError('GetClientRect failed')
        p=wintypes.POINT(0,0)
        if not user32.ClientToScreen(self.hwnd,ctypes.byref(p)):raise OSError('ClientToScreen failed')
        if rect.right<=0 or rect.bottom<=0:raise RuntimeError('no visible client area')
        with mss.mss() as s:
            bgra=np.asarray(s.grab(dict(left=p.x,top=p.y,width=rect.right,height=rect.bottom)))
        return bgra[:,:,:3].copy()
