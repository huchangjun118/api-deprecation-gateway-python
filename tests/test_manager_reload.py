"""Tests for hot reload: atomic snapshot swap, failed reload isolation,
concurrent reloads and concurrent request/reload traffic."""

import threading
from datetime import datetime, timezone

import pytest

from gateway.clock import FakeClock
from gateway.config import parse_policy
from gateway.errors import PolicyError
from gateway.manager import PolicyManager
from gateway.policy import LifecycleState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def policy_doc(versions):
    return {
        "min_deprecation_window_days": 30,
        "versions": versions,
    }


def v3_available():
    return [
        {
            "version": 3,
            "state": "available",
            "introduced_at": "2025-01-01T00:00:00Z",
        }
    ]


def v2_deprecated():
    return [
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
    ]


def v1_offline():
    return [
        {
            "version": 1,
            "state": "offline",
            "introduced_at": "2023-01-01T00:00:00Z",
            "deprecated_at": "2023-06-01T00:00:00Z",
            "sunset_at": "2024-01-01T00:00:00Z",
            "successor": 2,
        },
        {
            "version": 2,
            "state": "available",
            "introduced_at": "2024-01-01T00:00:00Z",
        },
    ]


def test_reload_swaps_snapshot():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v3_available()), now=NOW), clock=clock)
    assert set(manager.current().versions) == {3}
    manager.reload(parse_policy(policy_doc(v2_deprecated()), now=NOW))
    assert set(manager.current().versions) == {2, 3}


def test_failed_reload_keeps_active_policy():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v3_available()), now=NOW), clock=clock)
    active_before = manager.current()
    with pytest.raises(PolicyError):
        manager.reload_document({"versions": []})
    assert manager.current() is active_before
    assert set(manager.current().versions) == {3}


def test_failed_reload_with_bad_state_keeps_active():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v3_available()), now=NOW), clock=clock)
    with pytest.raises(PolicyError):
        manager.reload_document(
            policy_doc(
                [
                    {
                        "version": 9,
                        "state": "bogus",
                        "introduced_at": "2025-01-01T00:00:00Z",
                    }
                ]
            )
        )
    assert set(manager.current().versions) == {3}


def test_request_uses_single_snapshot_across_reload():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v2_deprecated()), now=NOW), clock=clock)

    observed = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            policy = manager.current()
            cfg = policy.get(2)
            if cfg is None:
                observed.append("gone")
            else:
                state = manager.effective_state(policy, 2)
                # Re-read policy through the same snapshot: must be consistent.
                assert policy.get(2) is cfg
                observed.append(state.value)

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(200):
        manager.reload(parse_policy(policy_doc(v2_deprecated()), now=NOW))
    stop.set()
    thread.join()
    assert observed, "reader never observed a state"
    assert "gone" not in observed


def test_concurrent_reloads_are_serialized_and_consistent():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v3_available()), now=NOW), clock=clock)
    errors = []

    def worker(i):
        try:
            doc = policy_doc(v2_deprecated() if i % 2 else v3_available())
            manager.reload(parse_policy(doc, now=NOW))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # Final state must be one of the two valid policies.
    assert set(manager.current().versions) in ({3}, {2, 3})


def test_slow_request_is_not_blocked_by_reload():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v2_deprecated()), now=NOW), clock=clock)

    policy = manager.current()  # snapshot held by an "in-flight request"
    reload_done = threading.Event()

    def reloader():
        for _ in range(50):
            manager.reload(parse_policy(policy_doc(v2_deprecated()), now=NOW))
        reload_done.set()

    thread = threading.Thread(target=reloader)
    thread.start()
    reload_done.wait(timeout=5)
    thread.join()
    # The in-flight snapshot is still usable and self-consistent.
    assert policy.get(2).state is LifecycleState.DEPRECATED
    assert manager.effective_state(policy, 2) is LifecycleState.DEPRECATED


def test_high_water_is_per_version_and_survives_reload():
    clock = FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_doc(v2_deprecated()), now=NOW), clock=clock)
    old_policy = manager.current()
    assert manager.effective_state(old_policy, 2) is LifecycleState.DEPRECATED

    # A reload never resets the protection of a version already observed,
    # even if the new policy declares that version as plain available...
    new_policy = parse_policy(
        policy_doc(
            [
                {
                    "version": 2,
                    "state": "available",
                    "introduced_at": "2024-01-01T00:00:00Z",
                },
                {
                    "version": 3,
                    "state": "available",
                    "introduced_at": "2025-01-01T00:00:00Z",
                },
            ]
        ),
        now=NOW,
    )
    manager.reload(new_policy)
    assert manager.effective_state(new_policy, 2) is LifecycleState.DEPRECATED
    # ...while a version never observed before starts fresh.
    assert manager.effective_state(new_policy, 3) is LifecycleState.AVAILABLE
