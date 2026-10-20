"""Independent, positively verified game-state evidence for automatic input.

Text acquisition and audio completion cannot manufacture an InputGrant or an
acknowledgement. Optional visual markers are merely one game-specific provider.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class GateState(Enum):
    UNKNOWN = 'unknown'
    ALLOW = 'verified_normal_advance'
    MANUAL_REQUIRED = 'manual_required'
    BLOCKED = 'blocked'


@dataclass(frozen=True)
class GateObservation:
    at_ms: int
    state: GateState
    proof_id: str = ''
    source: str = ''

    @property
    def may_advance(self) -> bool:
        return self.state is GateState.ALLOW and bool(self.proof_id and self.source)


class ClosedGate:
    """Safe standard; a game needs an independently validated progression signal."""
    def observe(self, now_ms: int) -> GateObservation:
        return GateObservation(now_ms, GateState.UNKNOWN)


class VisualGate:
    """Opt-in visual provider. Calibrated marker absence alone grants nothing."""
    def __init__(self, screen_observer):
        self.observer = screen_observer

    def observe(self, now_ms: int) -> GateObservation:
        from .contracts import ScreenState
        sample = self.observer.observe(now_ms)
        if sample.state is ScreenState.MANUAL_REQUIRED:
            return GateObservation(now_ms, GateState.MANUAL_REQUIRED, 'visual-manual', 'calibrated-visual')
        if (sample.state is ScreenState.ADVANCE_INDICATOR and sample.ready and sample.positive
                and sample.evidence == 'advance_allowed-marker'):
            # Only an explicit opt-in calibration can establish this result.
            return GateObservation(now_ms, GateState.ALLOW, 'visual-ready', 'calibrated-visual')
        return GateObservation(now_ms, GateState.UNKNOWN)
