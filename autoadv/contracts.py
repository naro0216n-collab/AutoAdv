"""Public event and state contracts for the single advance authority.

Timestamps use one monotonic clock in milliseconds. A caller must not map
clock values between different processes without a clock-synchronization layer.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class ContentKind(Enum):
    SPEAKABLE = auto()
    SYMBOLIC = auto()
    NON_DIALOGUE = auto()


class ScreenState(Enum):
    DIALOGUE = auto()
    MANUAL_REQUIRED = auto()
    TRANSITION = auto()
    UNKNOWN = auto()


class VoicePresence(Enum):
    PRESENT = auto()
    ABSENT = auto()
    UNRESOLVED = auto()


class Playback(Enum):
    NOT_STARTED = auto()
    ACTIVE = auto()
    COMPLETE = auto()
    UNKNOWN = auto()


class Evidence(Enum):
    METADATA = auto()
    AUDIO_CONFIRMED = auto()
    HEURISTIC = auto()
    NONE = auto()


class TTSPolicy(Enum):
    SELECTED = auto()
    DEFAULT = auto()
    SKIP = auto()
    DISABLED = auto()
    UNASSIGNED = auto()
    ERROR = auto()


class Presentation(Enum):
    NATIVE = auto()
    TTS = auto()
    SILENT = auto()
    UNRESOLVED = auto()


class ControllerState(Enum):
    WAIT_LINE = auto()
    WAIT_TEXT_READY = auto()
    RESOLVE_PRESENTATION = auto()
    WAIT_NATIVE_END = auto()
    WAIT_TTS_END = auto()
    WAIT_SILENT_DWELL = auto()
    READY_TO_ADVANCE = auto()
    WAIT_TRANSITION_ACK = auto()
    WAIT_MANUAL_CHOICE = auto()
    PAUSED = auto()


class EventKind(Enum):
    LINE_CANDIDATE = auto()
    LINE_UPDATED = auto()
    LINE_READY = auto()
    LINE_INVALIDATED = auto()
    SCREEN_CHANGED = auto()
    CAPTURE_HEALTH = auto()
    VOICE_PRESENCE = auto()
    NATIVE_STARTED = auto()
    NATIVE_COMPLETED = auto()
    TTS_FINISHED = auto()
    TIMER = auto()
    TRANSITION_ACK = auto()
    MANUAL_OVERRIDE = auto()
    FOCUS_CHANGED = auto()


@dataclass(frozen=True, slots=True)
class Envelope:
    kind: EventKind
    session_id: str
    at_ms: int
    # Asynchronous callbacks MUST carry the line generation and cancellation token.
    line_epoch: Optional[int] = None
    cancel_token: Optional[int] = None
    source_seq: Optional[int] = None
    text: Optional[str] = None
    source_line_id: Optional[str] = None
    text_ready: bool = False
    screen: Optional[ScreenState] = None
    capture_ok: Optional[bool] = None
    voice: Optional[VoicePresence] = None
    evidence: Evidence = Evidence.NONE
    native_utterance_id: Optional[str] = None
    native_asset_id: Optional[str] = None
    # A completed TTS event means that the audio output device has drained,
    # not merely that the HTTP synthesis request finished.
    tts_success: Optional[bool] = None
    focused: Optional[bool] = None
    # An ACK must originate from a confirmed line/scene boundary, not text hash.
    transition_id: Optional[str] = None


class EffectKind(Enum):
    START_TTS = auto()
    CANCEL_TTS = auto()
    REQUEST_ADVANCE = auto()
    STOPPED = auto()


@dataclass(frozen=True, slots=True)
class Effect:
    kind: EffectKind
    session_id: str
    line_epoch: int
    cancel_token: int
    # START_TTS payload; voice None is forbidden here.
    text: str = ""
    voice_id: Optional[str] = None
    reason: str = ""