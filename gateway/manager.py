"""Policy manager: hot-reload with RCU-style atomic snapshot swap.

* Requests read the active policy reference once at entry and use that single
  snapshot for the whole request -> every request observes a self-consistent
  policy, even while a reload is in progress.
* Reloads build and validate the candidate policy *before* swapping; a failed
  reload leaves the active policy untouched.
* In-flight requests never block a reload: the swap is a single reference
  assignment, old snapshots are released naturally once requests finish.
* Lifecycle states are derived from the injected clock and are monotonic
  per *version* across the whole lifetime of the manager: once a version
  has been observed in a state, neither a clock rollback nor a policy
  reload (including reloading identical content, or a failed reload) can
  ever make it available at an earlier state again.  The high-water
  record is keyed by version number only, so its size is bounded by the
  number of versions, not by the number of reloads.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from .clock import Clock
from .errors import PolicyError
from .policy import LifecycleState, Policy, VersionConfig, state_for_rank
from .stats import VersionStats

__all__ = ["PolicyManager"]


class PolicyManager:
    """Holds the active policy and coordinates reloads."""

    def __init__(
        self,
        policy: Policy,
        clock: Clock,
        *,
        stats: VersionStats | None = None,
    ) -> None:
        self._clock = clock
        self._policy = policy
        self._stats = stats or VersionStats()
        self._reload_lock = threading.Lock()
        # version -> highest state rank ever observed for it.  Keyed by
        # version number (never by policy object), so the protection
        # survives reloads and its size is bounded by the version count.
        self._high_water: dict[int, int] = {}
        # Guards high-water updates so concurrent requests can never lose
        # a monotonic increase; held only for a dict lookup + store.
        self._high_water_lock = threading.Lock()

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def stats(self) -> VersionStats:
        return self._stats

    def current(self) -> Policy:
        """Return the currently active policy snapshot."""
        return self._policy

    def reload(self, candidate: Policy) -> Policy:
        """Atomically replace the active policy with ``candidate``.

        The candidate must already be validated (see :func:`parse_policy`).
        Returns the new active policy.
        """
        if not isinstance(candidate, Policy):
            raise PolicyError("reload candidate must be a Policy instance")
        with self._reload_lock:
            # Reference assignment is atomic under the GIL; taking the lock
            # only serializes concurrent reloads, never requests.
            self._policy = candidate
        return self._policy

    def reload_document(self, document: dict[str, Any]) -> Policy:
        """Validate and swap in a new policy document.

        On validation failure the active policy is unchanged and
        :class:`PolicyError` propagates to the caller.
        """
        from .config import parse_policy

        candidate = parse_policy(document, now=self._clock.now())
        return self.reload(candidate)

    def reload_file(self, path: str) -> Policy:
        """Load a policy file, validate it and swap it in."""
        from .config import load_policy_file

        candidate = load_policy_file(path, now=self._clock.now())
        return self.reload(candidate)

    # -- lifecycle -------------------------------------------------------

    @staticmethod
    def derive_state(cfg: VersionConfig, now: datetime) -> LifecycleState:
        """Derive the lifecycle state of ``cfg`` at ``now``.

        Boundary semantics: ``now == deprecated_at`` is deprecated,
        ``now == sunset_at`` is offline (>= comparisons).
        """
        if cfg.sunset_at is not None and now >= cfg.sunset_at:
            return LifecycleState.OFFLINE
        if cfg.deprecated_at is not None and now >= cfg.deprecated_at:
            return LifecycleState.DEPRECATED
        if cfg.sunset_at is not None:
            return LifecycleState.PLANNED_OFFLINE
        return LifecycleState.AVAILABLE

    def effective_state(self, policy: Policy, version: int) -> LifecycleState:
        """Derive the effective state, clamped to the high-water mark.

        A version that reached OFFLINE (or DEPRECATED) stays at least at that
        rank even if the clock is rolled back afterwards — and even if the
        policy is reloaded (with identical or different content) in between.
        The clamp is applied under a lock so concurrent requests and reloads
        can never open a window in which the protection briefly disappears.
        """
        cfg = policy.require(version)
        derived = self.derive_state(cfg, self._clock.now())
        with self._high_water_lock:
            rank = max(derived.rank, self._high_water.get(version, 0))
            self._high_water[version] = rank
        return state_for_rank(rank)

    def record_decision(self, version: int, state: LifecycleState) -> None:
        """Account one version decision in the counters."""
        self._stats.record(version, state)
