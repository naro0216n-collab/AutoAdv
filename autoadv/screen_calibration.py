"""Positive-evidence template capture, keyed by stable game/profile identity.

The stored rectangle is expressed in client-area physical pixels, not desktop
coordinates. If the client size changes the operator must re-check calibration;
we never infer a new permission from scaling an old screenshot.
"""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import hashlib

from .screen import Marker, ROLES, recognize_markers
from .contracts import ScreenState


def validate_roi(frame, rect):
    if len(rect)!=4 or any(type(v) is not int for v in rect):
        raise ValueError('ROI coordinates must be integers')
    x,y,w,h=rect
    if getattr(frame,'ndim',0)!=3 or frame.shape[2]!=3:
        raise ValueError('expected captured BGR client image')
    if x<0 or y<0 or w<=0 or h<=0 or x+w>frame.shape[1] or y+h>frame.shape[0]:
        raise ValueError('ROI is outside the captured client')
    return frame[y:y+h,x:x+w].copy()


def save_calibration(frame, rect, target:Path, *, role:str):
    """Persist a fresh, exact template with atomic replacement after validation."""
    if role not in ROLES:raise ValueError('unknown screen evidence role')
    import cv2
    target=Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    encoded=cv2.imencode('.png',validate_roi(frame,rect))[1]
    fd,filename=tempfile.mkstemp(prefix='.marker-',suffix='.png',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(encoded.tobytes())
            output.flush();os.fsync(output.fileno())
        os.replace(filename,target)
    finally:
        if os.path.exists(filename):os.unlink(filename)
    return target


def probe(frame,document):
    """One sample of the very same matcher as runtime, not auto-authorization."""
    import cv2
    templates={}
    scores={}
    calibrated=document.get('client_size')
    if calibrated is not None and tuple(calibrated)!=(frame.shape[1],frame.shape[0]):
        return ScreenState.UNKNOWN,False,"client size changed; recalibration required",{}
    for role,record in document.get('markers',{}).items():
        if role not in ROLES:raise ValueError('unknown marker role')
        marker=Marker(Path(record['image']),tuple(record['rect']),record.get('min_score',.96),
                      tuple(record['source_client_size']) if record.get('source_client_size') else None)
        if calibrated is not None and marker.source_client_size!=tuple(calibrated):
            raise ValueError(f'marker {role} was calibrated at a different client size')
        image=cv2.imread(str(marker.image),cv2.IMREAD_COLOR)
        if image is None:raise ValueError(f'failed to read marker {role}')
        current=validate_roi(frame,marker.rect)
        if image.shape!=current.shape:raise ValueError('template and current ROI have different sizes')
        import numpy as np
        scores[role]=1-float(np.mean(np.abs(current.astype('float32')-image.astype('float32'))))/255
        templates[role]=(marker,image)
    state,ready,reason=recognize_markers(frame,templates)
    return state,ready,reason,scores


def immutable_calibration_path(frame,rect,root:Path,*,role:str)->Path:
    """New capture never overwrites a template referenced by the saved profile."""
    if role not in ROLES:raise ValueError('unknown screen evidence role')
    import cv2
    region=validate_roi(frame,rect)
    ok,image=cv2.imencode('.png',region)
    if not ok:raise ValueError('unable to encode captured marker')
    digest=hashlib.sha256(image.tobytes()).hexdigest()[:20]
    return Path(root) / f'{role}-{digest}.png'
