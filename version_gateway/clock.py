"""Injectable clock abstraction.

All time-dependent decisions go through a :class:`Clock` so that request
handling, policy reload and tests are deterministic for a fixed clock.
Every clock returns timezone-aware UTC instants; naive datetimes are
never produced by this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current instant as a timezone-aware datetime."""


class SystemClock:
    """Wall clock returning timezone-aware UTC instants."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Deterministic clock for tests and replayable request sequences."""

    def __init__(self, start: datetime) -> None:
        self._now = self._ensure_aware(start)

    @staticmethod
    def _ensure_aware(instant: datetime) -> datetime:
        if instant.tzinfo is None:
            # Treat naive input as UTC rather than guessing a local zone.
            return instant.replace(tzinfo=timezone.utc)
        return instant

    def set(self, instant: datetime) -> None:
        self._now = self._ensure_aware(instant)

    def advance(self, delta) -> None:
        self._now = self._now + delta

    def now(self) -> datetime:
        return self._now
