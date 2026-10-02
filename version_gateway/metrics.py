"""Per-version invocation counters for observing deprecation traffic."""

from __future__ import annotations

import threading

STATE_KEYS = ("available", "deprecated", "planned-retirement", "retired",
              "rejected")


class VersionMetrics:
    """Thread-safe counters ``counters[version][effective_state]``.

    Served deprecated traffic lands under the deprecated/planned-retirement
    buckets; requests refused for a retired/unknown/missing version land
    under ``rejected`` so dashboards can see both sides.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, int]] = {}

    def _bucket(self, version: str) -> dict[str, int]:
        bucket = self._counters.get(version)
        if bucket is None:
            bucket = {key: 0 for key in STATE_KEYS}
            self._counters[version] = bucket
        return bucket

    def record(self, version: str, state: str) -> None:
        key = state if state in STATE_KEYS else "rejected"
        with self._lock:
            self._bucket(version)[key] += 1

    def record_decision(self, decision) -> None:
        self.record(decision.version, decision.effective_state.value)

    def record_rejection(self, version: str | None) -> None:
        self.record(version if version is not None else "(unspecified)",
                    "rejected")

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Deep copy of all counters, safe to iterate while traffic flows."""
        with self._lock:
            return {version: dict(counts)
                    for version, counts in self._counters.items()}

    def totals(self) -> dict[str, int]:
        with self._lock:
            return {version: sum(counts.values())
                    for version, counts in self._counters.items()}

    def total_for(self, version: str) -> int:
        with self._lock:
            return sum(self._counters.get(version, {}).values())

    def deprecated_versions(self) -> dict[str, int]:
        """Versions still receiving deprecated traffic and their counts."""
        with self._lock:
            return {
                version: counts["deprecated"] + counts["planned-retirement"]
                for version, counts in self._counters.items()
                if counts["deprecated"] + counts["planned-retirement"] > 0
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
