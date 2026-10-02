"""Policy model: lifecycle states, per-version config and the policy snapshot.

A :class:`Policy` is an immutable snapshot of the version set.  Hot reload
builds a new policy object and atomically swaps it in; in-flight requests keep
referencing the snapshot they started with, so every request observes exactly
one self-consistent policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping


class LifecycleState(str, Enum):
    """Lifecycle state of a single API version.

    Order matters: states are listed in the order a version normally moves
    through them.  The gateway never lets a version move *backwards* within a
    policy snapshot (a rolled-back clock cannot resurrect a retired version).
    """

    AVAILABLE = "available"
    PLANNED_OFFLINE = "planned_offline"
    DEPRECATED = "deprecated"
    OFFLINE = "offline"

    @property
    def rank(self) -> int:
        return _STATE_RANK[self]


_STATE_RANK = {
    LifecycleState.AVAILABLE: 0,
    LifecycleState.PLANNED_OFFLINE: 1,
    LifecycleState.DEPRECATED: 2,
    LifecycleState.OFFLINE: 3,
}

_RANK_TO_STATE = {rank: state for state, rank in _STATE_RANK.items()}


def state_for_rank(rank: int) -> LifecycleState:
    """Return the lifecycle state for a monotonic rank value."""
    return _RANK_TO_STATE[rank]


@dataclass(frozen=True)
class VersionConfig:
    """Declared configuration of a single API version.

    All datetimes are timezone-aware UTC.
    """

    version: int
    state: LifecycleState
    introduced_at: datetime
    deprecated_at: datetime | None = None
    sunset_at: datetime | None = None
    successor: int | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be a positive integer")
        for name in ("introduced_at", "deprecated_at", "sunset_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    def has_sunset(self) -> bool:
        return self.sunset_at is not None


@dataclass(frozen=True)
class Policy:
    """Immutable version policy snapshot."""

    versions: Mapping[int, VersionConfig]
    min_deprecation_window: timedelta = field(default_factory=lambda: timedelta(days=30))
    source: str = "default"

    def __post_init__(self) -> None:
        # Freeze the mapping into a dict so the snapshot is truly immutable.
        object.__setattr__(self, "versions", dict(self.versions))

    def get(self, version: int) -> VersionConfig | None:
        return self.versions.get(version)

    def require(self, version: int) -> VersionConfig:
        cfg = self.versions.get(version)
        if cfg is None:
            raise KeyError(version)
        return cfg

    def sorted_versions(self) -> list[VersionConfig]:
        return [self.versions[v] for v in sorted(self.versions)]

    def successor_of(self, version: int) -> VersionConfig | None:
        cfg = self.versions.get(version)
        if cfg is None or cfg.successor is None:
            return None
        return self.versions.get(cfg.successor)
