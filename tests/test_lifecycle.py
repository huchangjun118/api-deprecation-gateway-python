"""Tests for lifecycle derivation: boundary moments, clock rollback and
timezone changes."""

from datetime import datetime, timedelta, timezone

import pytest

from gateway.clock import FakeClock
from gateway.config import parse_policy
from gateway.manager import PolicyManager
from gateway.policy import LifecycleState

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
DEPRECATION_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
SUNSET_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)


def make_manager(clock, *, deprecated_at=DEPRECATION_AT, sunset_at=SUNSET_AT):
    """Build a policy whose v2 lifecycle is consistent with ``clock``."""
    now = clock.now()
    if sunset_at is not None and now >= sunset_at:
        state = "offline"
    elif deprecated_at is not None and now >= deprecated_at:
        state = "deprecated"
    elif sunset_at is not None:
        state = "planned_offline"
    else:
        state = "available"

    versions = [
        {
            "version": 1,
            "state": "available",
            "introduced_at": (now - timedelta(days=400)).isoformat(),
        },
        {
            "version": 2,
            "state": state,
            "introduced_at": (now - timedelta(days=400)).isoformat(),
            "successor": 1,
        },
    ]
    if deprecated_at is not None:
        versions[1]["deprecated_at"] = deprecated_at.isoformat()
    if sunset_at is not None:
        versions[1]["sunset_at"] = sunset_at.isoformat()

    policy = parse_policy(
        {"min_deprecation_window_days": 30, "versions": versions},
        now=now,
    )
    return PolicyManager(policy, clock=clock)


def test_available_before_deprecation():
    clock = FakeClock(datetime(2025, 12, 31, tzinfo=timezone.utc))
    manager = make_manager(clock, sunset_at=None)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.AVAILABLE


def test_exactly_at_deprecation_is_deprecated():
    clock = FakeClock(DEPRECATION_AT)
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.DEPRECATED


def test_deprecated_between_dates():
    clock = FakeClock(datetime(2026, 3, 15, tzinfo=timezone.utc))
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.DEPRECATED


def test_exactly_at_sunset_is_offline():
    clock = FakeClock(SUNSET_AT)
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.OFFLINE


def test_after_sunset_is_offline():
    clock = FakeClock(datetime(2026, 12, 31, tzinfo=timezone.utc))
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.OFFLINE


def test_planned_offline_when_sunset_announced_but_not_deprecated():
    clock = FakeClock(datetime(2025, 6, 1, tzinfo=timezone.utc))
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.PLANNED_OFFLINE


def test_clock_rollback_does_not_resurrect_offline_version():
    clock = FakeClock(datetime(2026, 12, 1, tzinfo=timezone.utc))
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.OFFLINE
    # Roll the clock back before the deprecation instant.
    clock.set(datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert manager.effective_state(policy, 2) is LifecycleState.OFFLINE


def test_clock_rollback_does_not_downgrade_deprecated_version():
    clock = FakeClock(datetime(2026, 3, 1, tzinfo=timezone.utc))
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.DEPRECATED
    clock.set(datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert manager.effective_state(policy, 2) is LifecycleState.DEPRECATED


def test_timezone_change_keeps_same_instant():
    # Same instant expressed in a different timezone must not change the
    # decision (timestamps are normalized to UTC).
    clock_utc = FakeClock(datetime(2026, 3, 1, tzinfo=timezone.utc))
    clock_plus5 = FakeClock(datetime(2026, 3, 1, 5, tzinfo=timezone(timedelta(hours=5))))
    manager_utc = make_manager(clock_utc)
    manager_plus5 = make_manager(clock_plus5)
    state_utc = manager_utc.effective_state(manager_utc.current(), 2)
    state_plus5 = manager_plus5.effective_state(manager_plus5.current(), 2)
    assert state_utc is state_plus5 is LifecycleState.DEPRECATED


def test_naive_clock_datetime_is_assumed_utc():
    clock = FakeClock(datetime(2026, 3, 1))
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.DEPRECATED


def test_state_rank_ordering():
    assert LifecycleState.AVAILABLE.rank < LifecycleState.PLANNED_OFFLINE.rank
    assert LifecycleState.PLANNED_OFFLINE.rank < LifecycleState.DEPRECATED.rank
    assert LifecycleState.DEPRECATED.rank < LifecycleState.OFFLINE.rank
