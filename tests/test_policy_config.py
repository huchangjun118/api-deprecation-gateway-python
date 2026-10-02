"""Tests for policy loading and validation (state/metadata contradictions,
window violations, malformed documents)."""

from datetime import datetime, timedelta, timezone

import pytest

from gateway.clock import FakeClock
from gateway.config import default_policy, parse_policy
from gateway.errors import PolicyError
from gateway.policy import LifecycleState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def doc(**overrides):
    base = {
        "min_deprecation_window_days": 30,
        "versions": [
            {
                "version": 2,
                "state": "deprecated",
                "introduced_at": "2024-01-01T00:00:00Z",
                "deprecated_at": "2025-01-01T00:00:00Z",
                "sunset_at": "2026-06-01T00:00:00Z",
                "successor": 3,
            },
            {
                "version": 3,
                "state": "available",
                "introduced_at": "2025-01-01T00:00:00Z",
            },
        ],
    }
    base.update(overrides)
    return base


def test_valid_policy_loads():
    policy = parse_policy(doc(), now=NOW)
    assert set(policy.versions) == {2, 3}
    assert policy.get(2).state is LifecycleState.DEPRECATED
    assert policy.get(3).state is LifecycleState.AVAILABLE
    assert policy.get(2).successor == 3


def test_default_policy_is_consistent():
    clock = FakeClock(NOW)
    policy = default_policy(now=clock.now())
    assert policy.get(1).state is LifecycleState.OFFLINE
    assert policy.get(2).state is LifecycleState.DEPRECATED
    assert policy.get(3).state is LifecycleState.AVAILABLE


def test_available_with_deprecation_dates_rejected():
    bad = doc()
    bad["versions"][1]["deprecated_at"] = "2025-06-01T00:00:00Z"
    with pytest.raises(PolicyError, match="available"):
        parse_policy(bad, now=NOW)


def test_deprecated_without_deprecated_at_rejected():
    bad = doc()
    bad["versions"][0]["state"] = "deprecated"
    del bad["versions"][0]["deprecated_at"]
    with pytest.raises(PolicyError, match="deprecated_at"):
        parse_policy(bad, now=NOW)


def test_deprecated_without_sunset_rejected():
    bad = doc()
    del bad["versions"][0]["sunset_at"]
    with pytest.raises(PolicyError, match="sunset_at"):
        parse_policy(bad, now=NOW)


def test_deprecated_with_future_deprecation_rejected():
    bad = doc()
    bad["versions"][0]["deprecated_at"] = "2027-01-01T00:00:00Z"
    with pytest.raises(PolicyError, match="future"):
        parse_policy(bad, now=NOW)


def test_deprecated_but_sunset_passed_rejected():
    bad = doc()
    bad["versions"][0]["sunset_at"] = "2025-06-01T00:00:00Z"
    with pytest.raises(PolicyError, match="offline"):
        parse_policy(bad, now=NOW)


def test_offline_without_sunset_rejected():
    bad = doc()
    bad["versions"][0]["state"] = "offline"
    bad["versions"][0]["sunset_at"] = None
    with pytest.raises(PolicyError, match="sunset_at"):
        parse_policy(bad, now=NOW)


def test_offline_with_future_sunset_rejected():
    bad = doc()
    bad["versions"][0]["state"] = "offline"
    bad["versions"][0]["sunset_at"] = "2027-01-01T00:00:00Z"
    with pytest.raises(PolicyError, match="future"):
        parse_policy(bad, now=NOW)


def test_planned_offline_requires_sunset():
    bad = doc()
    bad["versions"][0]["state"] = "planned_offline"
    bad["versions"][0]["deprecated_at"] = None
    bad["versions"][0]["sunset_at"] = None
    with pytest.raises(PolicyError, match="sunset_at"):
        parse_policy(bad, now=NOW)


def test_planned_offline_with_past_deprecation_rejected():
    bad = doc()
    bad["versions"][0]["state"] = "planned_offline"
    bad["versions"][0]["deprecated_at"] = "2025-06-01T00:00:00Z"
    with pytest.raises(PolicyError, match="planned_offline"):
        parse_policy(bad, now=NOW)


def test_short_deprecation_window_rejected():
    # Window of ~516 days is shorter than the required 1000 days.
    bad = doc(min_deprecation_window_days=1000)
    with pytest.raises(PolicyError, match="window"):
        parse_policy(bad, now=NOW)


def test_window_boundary_is_accepted():
    # Exactly 30 days between deprecation and sunset: allowed (>=).
    d = doc(min_deprecation_window_days=30)
    d["versions"][0]["deprecated_at"] = "2025-12-22T00:00:00Z"
    d["versions"][0]["sunset_at"] = "2026-01-21T00:00:00Z"
    policy = parse_policy(d, now=NOW)
    assert policy is not None
    assert policy.get(2).sunset_at - policy.get(2).deprecated_at == timedelta(days=30)


def test_naive_timestamp_rejected():
    bad = doc()
    bad["versions"][0]["introduced_at"] = "2024-01-01T00:00:00"
    with pytest.raises(PolicyError, match="timezone-naive"):
        parse_policy(bad, now=NOW)


def test_bad_state_rejected():
    bad = doc()
    bad["versions"][1]["state"] = "retired"
    with pytest.raises(PolicyError, match="unknown lifecycle state"):
        parse_policy(bad, now=NOW)


def test_duplicate_version_rejected():
    bad = doc()
    bad["versions"].append(dict(bad["versions"][0]))
    with pytest.raises(PolicyError, match="duplicate"):
        parse_policy(bad, now=NOW)


def test_successor_missing_rejected():
    bad = doc()
    bad["versions"][0]["successor"] = 99
    with pytest.raises(PolicyError, match="successor"):
        parse_policy(bad, now=NOW)


def test_self_successor_rejected():
    bad = doc()
    bad["versions"][0]["successor"] = 2
    with pytest.raises(PolicyError, match="cannot succeed itself"):
        parse_policy(bad, now=NOW)


def test_empty_versions_rejected():
    with pytest.raises(PolicyError):
        parse_policy({"versions": []}, now=NOW)


def test_non_positive_window_rejected():
    with pytest.raises(PolicyError, match="positive"):
        parse_policy(doc(min_deprecation_window_days=0), now=NOW)


def test_sunset_before_introduced_rejected():
    bad = doc()
    bad["versions"][1]["sunset_at"] = "2024-06-01T00:00:00Z"
    with pytest.raises(PolicyError):
        parse_policy(bad, now=NOW)
