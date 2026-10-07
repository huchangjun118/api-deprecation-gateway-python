"""Tests for the monotonic offline protection across reloads and clock
rollbacks, its bounded memory footprint, concurrency safety and
deterministic replay."""

import threading
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway.clock import FakeClock
from gateway.config import parse_policy
from gateway.errors import PolicyError
from gateway.manager import PolicyManager
from gateway.middleware import VersionGatewayMiddleware
from gateway.policy import LifecycleState

AFTER_SUNSET = datetime(2026, 8, 1, tzinfo=timezone.utc)
BEFORE_SUNSET = datetime(2026, 3, 1, tzinfo=timezone.utc)  # deprecated window
V2_DEP = "2026-01-01T00:00:00Z"
V2_SUN = "2026-07-01T00:00:00Z"


def v2_doc(state: str) -> dict:
    """Policy document declaring v2 in ``state`` (v1 long offline, v3 fresh)."""
    return {
        "min_deprecation_window_days": 30,
        "versions": [
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
                "state": state,
                "introduced_at": "2024-01-01T00:00:00Z",
                "deprecated_at": V2_DEP,
                "sunset_at": V2_SUN,
                "successor": 3,
            },
            {
                "version": 3,
                "state": "available",
                "introduced_at": "2025-01-01T00:00:00Z",
            },
        ],
    }


def make_manager(clock: FakeClock, state: str = "offline") -> PolicyManager:
    return PolicyManager(parse_policy(v2_doc(state), now=clock.now()), clock=clock)


# --- rollback + reload: offline never resurrects --------------------------

def test_offline_survives_identical_reload_then_clock_rollback():
    clock = FakeClock(AFTER_SUNSET)
    manager = make_manager(clock)
    policy = manager.current()
    assert manager.effective_state(policy, 2) is LifecycleState.OFFLINE

    # Reloading byte-identical content must not reset the protection.
    manager.reload_document(v2_doc("offline"))
    # Rolling the clock back before the sunset must not either.
    clock.set(BEFORE_SUNSET)
    assert manager.effective_state(manager.current(), 2) is LifecycleState.OFFLINE


def test_offline_survives_clock_rollback_then_reload():
    clock = FakeClock(AFTER_SUNSET)
    manager = make_manager(clock)
    assert manager.effective_state(manager.current(), 2) is LifecycleState.OFFLINE

    clock.set(BEFORE_SUNSET)
    # At the rolled-back time a 'deprecated' declaration validates; the
    # reloaded policy would derive DEPRECATED, but the high-water mark wins.
    manager.reload_document(v2_doc("deprecated"))
    assert manager.current().versions[2].state is LifecycleState.DEPRECATED
    assert manager.effective_state(manager.current(), 2) is LifecycleState.OFFLINE


def test_failed_reload_preserves_protection():
    clock = FakeClock(AFTER_SUNSET)
    manager = make_manager(clock)
    assert manager.effective_state(manager.current(), 2) is LifecycleState.OFFLINE

    clock.set(BEFORE_SUNSET)
    with pytest.raises(PolicyError):
        manager.reload_document({"versions": []})
    assert manager.effective_state(manager.current(), 2) is LifecycleState.OFFLINE


def test_deprecated_mark_also_survives_reload_and_rollback():
    clock = FakeClock(BEFORE_SUNSET)
    manager = make_manager(clock, state="deprecated")
    assert manager.effective_state(manager.current(), 2) is LifecycleState.DEPRECATED

    manager.reload_document(v2_doc("deprecated"))
    clock.set(datetime(2025, 6, 1, tzinfo=timezone.utc))  # before deprecation
    assert manager.effective_state(manager.current(), 2) is LifecycleState.DEPRECATED


# --- bounded footprint -----------------------------------------------------

def test_high_water_record_bounded_by_version_count():
    clock = FakeClock(AFTER_SUNSET)
    manager = make_manager(clock)
    for _ in range(300):
        manager.reload_document(v2_doc("offline"))
        for version in (1, 2, 3):
            manager.effective_state(manager.current(), version)
    # One entry per version, no matter how many reloads happened.
    assert len(manager._high_water) == 3


# --- concurrency: no resurrection window -----------------------------------

def test_concurrent_reloads_and_requests_never_resurrect():
    clock = FakeClock(AFTER_SUNSET)
    manager = make_manager(clock)
    assert manager.effective_state(manager.current(), 2) is LifecycleState.OFFLINE
    clock.set(BEFORE_SUNSET)

    violations: list[LifecycleState] = []
    stop = threading.Event()

    def requester():
        while not stop.is_set():
            state = manager.effective_state(manager.current(), 2)
            if state is not LifecycleState.OFFLINE:
                violations.append(state)

    def reloader():
        for _ in range(200):
            manager.reload_document(v2_doc("deprecated"))
            # A failing reload must not open a window either.
            with pytest.raises(PolicyError):
                manager.reload_document({"versions": "not-a-list"})

    threads = [threading.Thread(target=requester) for _ in range(4)]
    reloader_thread = threading.Thread(target=reloader)
    for t in threads:
        t.start()
    reloader_thread.start()
    reloader_thread.join()
    stop.set()
    for t in threads:
        t.join()
    assert violations == []


# --- deterministic replay including reloads --------------------------------

def _run_script():
    """Run a fixed request/reload/clock script and record the conclusions."""
    clock = FakeClock(AFTER_SUNSET)
    manager = make_manager(clock)
    trace: list[tuple] = []

    def req(version: int) -> None:
        policy = manager.current()
        cfg = policy.get(version)
        state = None if cfg is None else manager.effective_state(policy, version)
        trace.append(("req", version, None if state is None else state.value))

    def reload_doc(state: str) -> None:
        try:
            manager.reload_document(v2_doc(state))
            trace.append(("reload", state, "ok"))
        except PolicyError:
            trace.append(("reload", state, "failed"))

    req(2)                                  # offline (after sunset)
    reload_doc("offline")                   # identical content
    req(2)
    clock.set(BEFORE_SUNSET)                # clock rollback
    req(2)                                  # still offline
    reload_doc("deprecated")                # validates at rolled-back time
    req(2)                                  # still offline
    reload_doc("offline")                   # invalid now: sunset in future
    req(2)
    req(3)                                  # unaffected version
    clock.set(AFTER_SUNSET)
    req(2)
    return trace


def test_replay_with_reloads_is_deterministic():
    first = _run_script()
    second = _run_script()
    assert first == second
    # Every v2 conclusion is offline despite the rollback and the reloads.
    assert [entry for entry in first if entry[0] == "req" and entry[1] == 2] == [
        ("req", 2, "offline")
    ] * 6
    assert ("reload", "offline", "failed") in first


# --- end to end through the middleware -------------------------------------

def build_app(clock: FakeClock):
    manager = PolicyManager(parse_policy(v2_doc("offline"), now=clock.now()), clock=clock)
    app = FastAPI()
    app.add_middleware(VersionGatewayMiddleware, manager=manager)

    @app.get("/v{version}/ping")
    def ping(version: int):
        return {"version": version, "message": "pong"}

    @app.post("/gateway/reload")
    async def reload(request: Request):
        body = await request.json()
        try:
            manager.reload_document(body)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "reload_failed", "message": str(exc)}},
            )
        return {"status": "reloaded"}

    return app, manager


def test_offline_version_stays_retired_after_reload_and_rollback_e2e():
    clock = FakeClock(AFTER_SUNSET)
    app, _ = build_app(clock)
    client = TestClient(app)

    r = client.get("/v2/ping")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"
    assert r.json()["error"]["details"]["successor"] == 3
    assert r.headers["Link"] == '</v3/>; rel="successor-version"'

    # Hot-reload identical content, then roll the clock back: still retired.
    assert client.post("/gateway/reload", json=v2_doc("offline")).status_code == 200
    clock.set(BEFORE_SUNSET)
    r = client.get("/v2/ping")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"
    assert r.headers["Link"] == '</v3/>; rel="successor-version"'

    # Reload a policy that (at the rolled-back time) only declares the
    # version deprecated: the retirement protection still wins.
    assert client.post("/gateway/reload", json=v2_doc("deprecated")).status_code == 200
    r = client.get("/v2/ping")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"

    # A failed reload does not lift the protection either.
    assert client.post("/gateway/reload", json={"versions": []}).status_code == 400
    r = client.get("/v2/ping")
    assert r.status_code == 410
