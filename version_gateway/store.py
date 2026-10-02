"""Hot-reloadable policy store with atomic snapshot replacement.

Concurrency model
-----------------
* ``reload`` fully validates the *new* policy before touching the active
  snapshot, so a failed reload leaves the in-effect policy untouched.
* The active snapshot is swapped with a single lock-protected reference
  assignment. Requests take the (brief) lock only to grab that
  reference; they then work on an immutable :class:`Policy`, so a slow
  request never blocks a reload and vice versa.
* Every request sees one self-consistent snapshot; it can never observe
  a half-loaded policy.

Clock anomalies
---------------
The store keeps a monotonically increasing "high-water mark" of every
instant it has observed across requests *and* across reloads. Version
decisions use ``max(clock.now(), high_water_mark)``: once an instant at
or after a ``sunset_at`` has been observed, a clock moving backwards
(or a timezone reinterpretation yielding an earlier UTC instant) can
never make a retired version available again.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from .clock import Clock, SystemClock
from .errors import PolicyValidationError
from .lifecycle import validate_policy
from .models import Policy


def _to_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise PolicyValidationError("naive datetime cannot enter the policy store")
    return instant.astimezone(timezone.utc)


class PolicyStore:
    def __init__(self, policy: Policy | None = None,
                 clock: Clock | None = None) -> None:
        self._lock = threading.RLock()
        self._policy: Policy | None = None
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._high_water: datetime | None = None
        if policy is not None:
            self.reload(policy)

    @property
    def clock(self) -> Clock:
        return self._clock

    def get(self) -> Policy | None:
        """Return the active immutable snapshot (or None before first load)."""
        with self._lock:
            return self._policy

    def require(self) -> Policy:
        policy = self.get()
        if policy is None:
            raise PolicyValidationError("no active version policy has been loaded")
        return policy

    def reload(self, policy: Policy) -> None:
        """Validate then atomically install ``policy``.

        Raises :class:`PolicyValidationError` without changing the active
        snapshot when the new policy is inconsistent.
        """
        validate_policy(policy)  # validate BEFORE mutating anything
        with self._lock:
            self._policy = policy

    def observed_time(self, clock: Clock | None = None) -> datetime:
        """Return the monotonic high-water instant and advance it.

        Reads never move the mark backwards, even if the supplied clock
        reports an earlier instant than a previous call.
        """
        source = clock if clock is not None else self._clock
        candidate = _to_utc(source.now())
        with self._lock:
            if self._high_water is None or candidate > self._high_water:
                self._high_water = candidate
            return self._high_water

    def high_water_mark(self) -> datetime | None:
        with self._lock:
            return self._high_water
