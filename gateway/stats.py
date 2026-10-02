"""Per-version call counters (thread-safe).

Counters are keyed by version and broken down by lifecycle state at decision
time, so operators can identify traffic still hitting deprecated / retired
versions.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from .policy import LifecycleState

__all__ = ["VersionStats"]


class VersionStats:
    """Process-wide per-version counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # version -> state -> count
        self._counts: dict[int, dict[LifecycleState, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._totals: dict[int, int] = defaultdict(int)

    def record(self, version: int, state: LifecycleState) -> None:
        """Record one decision for ``version`` observed in ``state``."""
        with self._lock:
            self._totals[version] += 1
            self._counts[version][state] += 1

    def total(self, version: int) -> int:
        with self._lock:
            return self._totals.get(version, 0)

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of all counters."""
        with self._lock:
            versions: dict[int, dict[str, int]] = {}
            for version in sorted(self._totals):
                state_counts = self._counts[version]
                versions[version] = {
                    "total": self._totals[version],
                    **{state.value: state_counts.get(state, 0) for state in LifecycleState},
                }
            return {"versions": versions}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._totals.clear()
