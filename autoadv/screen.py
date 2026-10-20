"""Positive visual permission, manual blocker and transition observations.

LunaHost supplies directly observed text. This adapter never OCRs text or classifies unnecessary
screen types. A missing marker is UNKNOWN, never permission.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import json
from .contracts import ScreenState

ROLES = frozenset(('manual_block','transition','advance_allowed'))


@dataclass(frozen=True)
class Marker:
    image:Path
    rect:tuple[int,int,int,int]
    min_score:float=.96
    source_client_size: tuple[int,int] | None = None

    def __post_init__(self):
        x,y,w,h=self.rect
        if min(x,y)<0 or w<=0 or h<=0 or not .5<=self.min_score<=1:
            raise ValueError('invalid marker ROI or threshold')
        if self.source_client_size is not None:
            if (len(self.source_client_size)!=2 or any(type(a) is not int or a<=0 for a in self.source_client_size)
                or x+w>self.source_client_size[0] or y+h>self.source_client_size[1]):
                raise ValueError('marker capture size inconsistent with ROI')


@dataclass(frozen=True)
class ScreenProfile:
    markers:dict[str,Marker]
    minimum_ready_observations:int=2
    minimum_ready_span_ms:int=120
    client_size: tuple[int,int] | None=None

    def __post_init__(self):
        if not self.markers or not set(self.markers).issubset(ROLES):
            raise ValueError('calibrated screen evidence requires one or more known markers')
        if self.minimum_ready_observations<2 or self.minimum_ready_span_ms<=0:
            raise ValueError('invalid ready calibration')
        if self.client_size is not None:
            if (len(self.client_size)!=2 or any(type(v) is not int or v<=0 for v in self.client_size)):
                raise ValueError('invalid calibrated client dimensions')
            if any(marker.source_client_size != self.client_size for marker in self.markers.values()):
                raise ValueError('markers were not all captured at this client size')

    @classmethod
    def load(cls,path:Path) -> 'ScreenProfile':
        data=json.loads(path.read_text(encoding='utf-8'))
        if set(data)-{'markers','minimum_ready_observations','minimum_ready_span_ms','client_size'}:
            raise ValueError('unknown screen profile fields')
        return cls({name:Marker((path.parent / d['image']).resolve(),tuple(d['rect']),d.get('min_score',.96),
                                 tuple(d['source_client_size']) if d.get('source_client_size') else None)
                    for name,d in data['markers'].items()},
                   data.get('minimum_ready_observations',2),data.get('minimum_ready_span_ms',120),
                   tuple(data['client_size']) if data.get('client_size') else None)


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
        # Positive evidence of a manual-only state always blocks input.
        return ScreenState.MANUAL_REQUIRED,False,'manual_block marker'
    if 'transition' in matched:
        if 'advance_allowed' in matched:
            return ScreenState.UNKNOWN,False,'conflicting transition and advance indicators'
        # Transition observation is NOT a transition ACK.
        return ScreenState.TRANSITION,False,'transition marker'
    if 'advance_allowed' in matched:
        return ScreenState.ADVANCE_INDICATOR,True,'advance_allowed-marker'
    return ScreenState.UNKNOWN,False,'no verified advance indicator'


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
        frame=self.capture()
        if (self.profile.client_size is not None and
            (frame.shape[1],frame.shape[0])!=self.profile.client_size):
            self._ready_first_at=None
            self._ready_count=0
            return ScreenSample(at_ms,ScreenState.UNKNOWN,False,False,'client size changed; recalibration required')
        state,ready_marker,evidence=recognize_markers(frame,self.templates)
        if state is ScreenState.ADVANCE_INDICATOR and ready_marker:
            if self._ready_first_at is None:self._ready_first_at=at_ms
            self._ready_count+=1
        else:
            self._ready_first_at=None
            self._ready_count=0
        stable=(self._ready_count>=self.profile.minimum_ready_observations and
                self._ready_first_at is not None and
                at_ms-self._ready_first_at>=self.profile.minimum_ready_span_ms)
        return ScreenSample(at_ms,state,stable,state is ScreenState.ADVANCE_INDICATOR and ready_marker,evidence)


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
