"""Hot reload: atomicity, failed-reload isolation and clock anomalies."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from version_gateway import FixedClock, Policy, PolicyStore, VersionState
from version_gateway.errors import PolicyValidationError

from .conftest import DEP, SUN, T0, make_policy, v1, v2


def test_first_policy_is_served(store, policy):
    assert store.get() is policy


def test_reload_swaps_snapshot(store):
    new_policy = make_policy(default_version="1")
    store.reload(new_policy)
    assert store.get() is new_policy


def test_invalid_reload_keeps_active_policy(store, policy):
    with pytest.raises(PolicyValidationError):
        store.reload(Policy(versions={}))
    assert store.get() is policy


def test_reload_with_contradictory_policy_is_isolated(store, policy):
    from version_gateway import VersionInfo
    bad = VersionInfo("1", VersionState.AVAILABLE, T0,
                      deprecated_at=DEP, sunset_at=SUN, successor="2")
    with pytest.raises(PolicyValidationError):
        store.reload(make_policy(versions={"1": bad, "2": v2()}))
    assert store.get() is policy


def test_requests_see_one_consistent_snapshot_under_contention(store):
    """Repeated reads during reloads always return a complete Policy."""
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            snapshot = store.get()
            # Snapshot is immutable; it must always be a validated policy.
            if snapshot is None or not snapshot.versions:
                errors.append("bad snapshot")

    def reloader():
        i = 0
        while not stop.is_set():
            i += 1
            store.reload(make_policy(default_version="1" if i % 2 else "2"))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads += [threading.Thread(target=reloader) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=2)
    assert errors == []


def test_slow_request_does_not_block_reload(store):
    # A request only borrows the immutable snapshot reference; reload must
    # complete immediately even while the "request" holds that reference.
    snapshot = store.get()
    start = time.monotonic()
    store.reload(make_policy(default_version="1"))
    elapsed = time.monotonic() - start
    assert elapsed < 0.1
    # The borrowed snapshot is still self-consistent afterwards.
    assert snapshot.get("1") is not None


def test_high_water_mark_advances_monotonically(store, clock):
    clock.set(T0 + timedelta(days=10))
    assert store.observed_time() == T0 + timedelta(days=10)
    clock.set(T0)  # clock moved backwards
    assert store.observed_time() == T0 + timedelta(days=10)


def test_high_water_survives_reload(store, clock):
    clock.set(DEP)
    store.observed_time()
    store.reload(make_policy(default_version="1"))
    clock.set(T0)
    assert store.observed_time() == DEP


def test_high_water_makes_retirement_sticky(store, clock):
    """After sunset was observed, rollback must not resurrect a version."""
    from version_gateway.negotiation import RequestView
    from version_gateway.resolver import resolve
    from version_gateway.errors import VersionNegotiationError

    clock.set(SUN)
    with pytest.raises(VersionNegotiationError, match="retired"):
        resolve(RequestView("GET", "/v1/x"), store, clock)
    clock.set(T0)
    with pytest.raises(VersionNegotiationError, match="retired"):
        resolve(RequestView("GET", "/v1/x"), store, clock)


def test_explicit_clock_argument_feeds_high_water(store):
    store.observed_time(FixedClock(T0 + timedelta(days=3)))
    assert store.high_water_mark() == T0 + timedelta(days=3)
