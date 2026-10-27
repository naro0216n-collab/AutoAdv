"""Factual acoustic measurements shared between measurement and inference layers.

These types preserve what was measured and the provenance needed to interpret it.
They do not describe Presentation completion, advancement policy, or source
ownership.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .forced_alignment import TargetTimeAlignment


@dataclass(frozen=True)
class VocalActivityInterval:
    """One observer-local interval of measured human-vocal activity.

    ``inactive_*`` records the contiguous sub-threshold support that established
    disappearance. It is measurement evidence, not a Presentation state.
    """

    start_ms: int
    last_active_ms: int
    inactive_required_ms: int
    inactive_start_ms: int | None = None
    inactive_end_ms: int | None = None

    @property
    def ongoing(self) -> bool:
        return (
            self.inactive_start_ms is None
            or self.inactive_end_ms is None
            or self.inactive_end_ms - self.inactive_start_ms < self.inactive_required_ms
        )

    def inactive_support_after(self, boundary_ms: int) -> bool:
        if self.ongoing or self.inactive_start_ms is None or self.inactive_end_ms is None:
            return False
        return (
            self.inactive_end_ms - max(self.inactive_start_ms, int(boundary_ms))
            >= self.inactive_required_ms
        )


@dataclass(frozen=True)
class VocalEvidenceSeries:
    """Observer-local human-vocal activity used by integrated inference."""

    observer: str
    activity: tuple[VocalActivityInterval, ...] = ()


@dataclass(frozen=True)
class HumanVocalEvidence:
    """Human-vocal measurements grouped only by observer provenance."""

    series: tuple[VocalEvidenceSeries, ...] = ()


@dataclass(frozen=True)
class VocalRecurrenceEvidence:
    """One periodic-repetition measurement from one observer/query."""

    observer: str = ""
    strength: float = 0.0
    period_ms: int | None = None
    prominence: float = 0.0


@dataclass(frozen=True)
class LexicalProgressEvidence:
    """Target-conditioned alignment measurement for one PCM query."""

    target_units: int
    observed_units: int
    aligned_target_units: int
    target_associated: bool
    complete_alignment: bool
    committed_target_end: bool
    target_start_ms: int | None
    aligned_end_ms: int | None
    target_end_ms: int | None
    reading_rank: int
    path_margin: float
    boundary_margin: float

    @classmethod
    def from_alignment(
        cls,
        alignment: TargetTimeAlignment,
        *,
        target_start_ms: int | None,
        aligned_end_ms: int | None,
        target_end_ms: int | None,
    ) -> "LexicalProgressEvidence":
        return cls(
            target_units=alignment.target_units,
            observed_units=alignment.observed_units,
            aligned_target_units=alignment.aligned_target_units,
            target_associated=alignment.target_associated,
            complete_alignment=alignment.complete_alignment,
            committed_target_end=alignment.committed_target_end,
            target_start_ms=target_start_ms,
            aligned_end_ms=aligned_end_ms,
            target_end_ms=target_end_ms,
            reading_rank=alignment.reading_rank,
            path_margin=alignment.fit.path_margin,
            boundary_margin=alignment.fit.boundary_margin,
        )
