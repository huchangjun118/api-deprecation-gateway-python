"""Injectable clocks used for lifecycle decisions.

The gateway never reads the wall clock directly.  Callers inject a
:class:`Clock` so that boundary moments (exact deprecation / sunset instants)
and clock anomalies (rollback, timezone changes) can be tested
deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Provides the current, timezone-aware UTC datetime."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Clock backed by the real wall clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Deterministic clock for tests.

    Time only moves forward explicitly via :meth:`advance` / :meth:`set`.
    ``now()`` always returns a timezone-aware UTC datetime.
    """

    def __init__(self, start: datetime | str | None = None) -> None:
        if start is None:
            value = datetime.now(timezone.utc)
        elif isinstance(start, str):
            value = datetime.fromisoformat(start)
        else:
            value = start
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._now = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime | str) -> None:
        """Set the clock to an explicit instant (timezone-aware)."""
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
        else:
            parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        self._now = parsed.astimezone(timezone.utc)

    def advance(
        self,
        *,
        days: float = 0,
        seconds: float = 0,
        microseconds: float = 0,
        milliseconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        weeks: float = 0,
    ) -> datetime:
        """Advance the clock by the given delta and return the new time."""
        delta = timedelta(
            days=days,
            seconds=seconds,
            microseconds=microseconds,
            milliseconds=milliseconds,
            minutes=minutes,
            hours=hours,
            weeks=weeks,
        )
        self._now = self._now + delta
        return self._now
