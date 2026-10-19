"""Adapters for tests and safe default runtime behavior."""
from __future__ import annotations
from .contracts import ScreenState
from .runtime import FreshSnapshot


class ClosedGuard:
    """Default until a game-specific, independently verified guard is provided."""
    def observe(self) -> FreshSnapshot:
        return FreshSnapshot(ScreenState.UNKNOWN, False, False, False)