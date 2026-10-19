"""Calibrated screenshot recognizer for dialogue, readiness and blockers.

The profile supplies *positive* visual markers for all screen categories.
Missing or ambiguous matches yield UNKNOWN. OCR of rendered text is a second
independent observation: a Luna pre-filter line packet never proves readiness.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import json
import unicodedata
from .contracts import ScreenState
from .audio_match import phonetic_key

ROLES = ('dialogue', 'text_ready', 'choice', 'map', 'menu', 'transition')
OPTIONAL_ROLES = ('empty_event',)
BLOCKERS = ('choice', 'map', 'menu', 'transition')


@dataclass(frozen=True)
class Marker:
    image: Path
    rect: tuple[int, int, int, int]  # x,y,width,height, client pixels
    min_score: float = .96

    def __post_init__(self):
        x,y,w,h=self.rect
        if min(x,y)<0 or w<=0 or h<=0 or not .5<=self.min_score<=1:
            raise ValueError('invalid marker ROI or threshold')


@dataclass(frozen=True)
class ScreenProfile:
    markers: dict[str,Marker]
    text_rect: tuple[int,int,int,int]
    minimum_ready_observations: int = 2
    minimum_ready_span_ms: int = 120

    def __post_init__(self):
        if not set(ROLES).issubset(self.markers) or not set(self.markers).issubset(set(ROLES)|set(OPTIONAL_ROLES)):
            raise ValueError('all dialogue/readiness/blocker markers are required')
        x,y,w,h=self.text_rect
        if min(x,y)<0 or w<=0 or h<=0 or self.minimum_ready_observations<2 or self.minimum_ready_span_ms<=0:
            raise ValueError('invalid text/ready calibration')

    @classmethod
    def load(cls,path:Path) -> 'ScreenProfile':
        obj=json.loads(path.read_text(encoding='utf-8'))
        base=path.parent
        return cls({name:Marker((base / data['image']).resolve(),tuple(data['rect']),data.get('min_score',.96))
                    for name,data in obj['markers'].items()}, tuple(obj['text_rect']),
                   obj.get('minimum_ready_observations',2),obj.get('minimum_ready_span_ms',120))


@dataclass(frozen=True)
class ScreenSample:
    at_ms: int
    state: ScreenState
    ready: bool
    text_matches: bool
    raw_ocr: str
    calibrated: bool
    empty_event_confirmed: bool = False


def visible_key(text: str) -> str:
    # Unlike ASR matching, punctuation and symbolic lines are semantically
    # significant on the display. Do not erase "……" during OCR comparison.
    return ''.join(c for c in unicodedata.normalize('NFKC',text)
                   if not c.isspace())


def recognize_markers(frame, templates:dict[str,tuple[Marker,object]]) -> tuple[ScreenState,bool]:
    import cv2
    import numpy as np
    matched=[]
    for role,(marker,template) in templates.items():
        x,y,w,h=marker.rect
        roi=frame[y:y+h,x:x+w]
        if roi.shape[:2]!=(h,w) or template.shape[:2]!=(h,w):
            return ScreenState.UNKNOWN,False
        diff=np.mean(np.abs(roi.astype('float32')-template.astype('float32')))
        score=1-diff/255
        if score>=marker.min_score:
            matched.append(role)
    blockers=[r for r in BLOCKERS if r in matched]
    if len(blockers)>1 or (blockers and 'dialogue' in matched):
        return ScreenState.UNKNOWN,False
    if blockers:
        return {'choice':ScreenState.CHOICE,'map':ScreenState.MAP,
                'menu':ScreenState.MENU,'transition':ScreenState.TRANSITION}[blockers[0]],False
    if 'dialogue' not in matched:
        return ScreenState.UNKNOWN,False
    return ScreenState.DIALOGUE,'text_ready' in matched


class ScreenObserver:
    def __init__(self, profile:ScreenProfile, capture:Callable[[],object],
                 ocr:Callable[[object],str] | None=None):
        import cv2
        self.profile,self.capture=profile,capture
        self.ocr=ocr or self._ocr_japanese
        self.templates={}
        for role, marker in profile.markers.items():
            arr=cv2.imread(str(marker.image),cv2.IMREAD_COLOR)
            if arr is None or arr.shape[:2]!=(marker.rect[3],marker.rect[2]):
                raise ValueError(f'calibration image missing/invalid for {role}')
            self.templates[role]=(marker,arr)
        self._ready_first_at=None
        self._ready_count=0

    @staticmethod
    def _ocr_japanese(image)->str:
        import pytesseract
        import cv2
        return pytesseract.image_to_string(cv2.cvtColor(image,cv2.COLOR_BGR2RGB),lang='jpn',config='--psm 6')

    def observe(self,expected_text:str,at_ms:int)->ScreenSample:
        frame=self.capture()  # This always grabs a FRESH image.
        state,ready_marker=recognize_markers(frame,self.templates)
        if state is ScreenState.DIALOGUE and ready_marker:
            if self._ready_first_at is None:
                self._ready_first_at=at_ms
            self._ready_count+=1
        else:
            self._ready_first_at=None
            self._ready_count=0
        stable=(self._ready_count>=self.profile.minimum_ready_observations and
                self._ready_first_at is not None and
                at_ms-self._ready_first_at>=self.profile.minimum_ready_span_ms)
        raw=''
        if state is ScreenState.DIALOGUE and stable:
            x,y,w,h=self.profile.text_rect
            roi=frame[y:y+h,x:x+w]
            if roi.shape[:2]==(h,w):
                raw=self.ocr(roi)
        # OCR is not trusted to identify different consecutive identical lines.
        empty_confirmed=False
        if not visible_key(expected_text) and 'empty_event' in self.templates and stable:
            marker,template=self.templates['empty_event']
            x,y,w,h=marker.rect
            roi=frame[y:y+h,x:x+w]
            if roi.shape[:2]==(h,w):
                import numpy as np
                score=1-np.mean(np.abs(roi.astype('float32')-template.astype('float32')))/255
                empty_confirmed=score>=marker.min_score
        match=(bool(visible_key(expected_text)) and
               visible_key(raw)==visible_key(expected_text)) or empty_confirmed
        return ScreenSample(at_ms,state,stable,match,raw,True,empty_confirmed)


class WindowsClientCapture:
    def __init__(self,hwnd:int,expected_pid:int):
        if hwnd<=0 or expected_pid<=0:
            raise ValueError('HWND and PID are required')
        self.hwnd,self.pid=hwnd,expected_pid

    def __call__(self):
        import sys
        if sys.platform!='win32':
            raise OSError('Windows screenshot capture required')
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
        if not user32.GetClientRect(self.hwnd,ctypes.byref(rect)):
            raise OSError('GetClientRect failed')
        p=wintypes.POINT(0,0)
        if not user32.ClientToScreen(self.hwnd,ctypes.byref(p)):
            raise OSError('ClientToScreen failed')
        if rect.right<=0 or rect.bottom<=0:
            raise RuntimeError('no visible client area')
        with mss.mss() as s:
            bgra=np.asarray(s.grab(dict(left=p.x,top=p.y,width=rect.right,height=rect.bottom)))
        return bgra[:,:,:3].copy()  # BGR
