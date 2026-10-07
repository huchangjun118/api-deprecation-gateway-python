"""Tests for the cross-reload monotonic lifecycle protection.

Once a version has been observed in a lifecycle state, neither a clock
rollback nor a policy reload (successful, identical-content or failed) may
move it backwards — in particular an offline version must never come back
to life.  The protection record must stay bounded by the number of
versions, not grow with the number of reloads.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway.clock import FakeClock
from gateway.config import parse_policy
from gateway.manager import PolicyManager
from gateway.middleware import VersionGatewayMiddleware
from gateway.policy import LifecycleState

SUNSET_V2 = datetime(2026, 7, 1, tzinfo=timezone.utc)
DEPRECATION_V2 = datetime(2026, 1, 1, tzinfo=timezone.utc)
AFTER_SUNSET = datetime(2026, 12, 1, tzinfo=timezone.utc)
BEFORE_DEPRECATION = datetime(2025, 6, 1, tzinfo=timezone.utc)


def policy_document():
    """Same content every time: v2 deprecated at DEPRECATION_V2, sunset at
    SUNSET_V2; declared states are filled in by the caller's clock."""
    return {
        "min_deprecation_window_days": 30,
        "versions": [
            {
                "version": 2,
                "state": "offline",
                "introduced_at": "2024-01-01T00:00:00Z",
                "deprecated_at": DEPRECATION_V2.isoformat(),
                "sunset_at": SUNSET_V2.isoformat(),
                "successor": 3,
            },
            {
                "version": 3,
                "state": "available",
                "introduced_at": "2025-01-01T00:00:00Z",
            },
        ],
    }


def build(now):
    """Build (client, manager, clock) with a policy consistent at ``now``."""
    clock = FakeClock(now)
    doc = policy_document()
    if clock.now() < SUNSET_V2:
        doc["versions"][0]["state"] = (
            "deprecated" if clock.now() >= DEPRECATION_V2 else "planned_offline"
        )
    manager = PolicyManager(parse_policy(doc, now=clock.now()), clock=clock)
    app = FastAPI()
    app.add_middleware(VersionGatewayMiddleware, manager=manager)

    @app.get("/v{version}/ping")
    def ping(version: int):
        return {"version": version, "message": "pong"}

    @app.post("/gateway/reload")
    async def reload_(request: Request):
        body = await request.json()
        try:
            manager.reload_document(body)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
        return {"status": "reloaded"}

    return TestClient(app), manager, clock


def reload_same_content(manager, clock):
    """Reload the identical policy document, validated against the clock."""
    doc = policy_document()
    if clock.now() < SUNSET_V2:
        doc["versions"][0]["state"] = (
            "deprecated" if clock.now() >= DEPRECATION_V2 else "planned_offline"
        )
    manager.reload_document(doc)


# --- rollback + reload: offline must stay offline --------------------------

def test_rollback_after_reload_keeps_version_retired():
    client, manager, clock = build(AFTER_SUNSET)
    r = client.get("/v2/ping")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"

    # Reload identical content, then roll the clock back before the sunset.
    reload_same_content(manager, clock)
    clock.set(BEFORE_DEPRECATION)

    r = client.get("/v2/ping")
    assert r.status_code == 410
    body = r.json()
    assert body["error"]["code"] == "version_retired"
    # Migration guidance is still attached.
    assert body["error"]["details"]["successor"] == 3
    assert r.headers["Link"] == '</v3/>; rel="successor-version"'


def test_reload_with_identical_content_does_not_reset_protection():
    client, manager, clock = build(AFTER_SUNSET)
    assert client.get("/v2/ping").status_code == 410

    clock.set(BEFORE_DEPRECATION)
    for _ in range(5):
        reload_same_content(manager, clock)
        assert client.get("/v2/ping").status_code == 410


def test_failed_reload_does_not_touch_protection():
    client, manager, clock = build(AFTER_SUNSET)
    assert client.get("/v2/ping").status_code == 410
    clock.set(BEFORE_DEPRECATION)

    # A failing reload must neither swap the policy nor clear the marks.
    r = client.post("/gateway/reload", json={"versions": []})
    assert r.status_code == 400
    assert client.get("/v2/ping").status_code == 410


def test_protection_record_bounded_by_version_count():
    client, manager, clock = build(AFTER_SUNSET)
    assert client.get("/v2/ping").status_code == 410
    assert client.get("/v3/ping").status_code == 200

    for _ in range(200):
        reload_same_content(manager, clock)
        client.get("/v2/ping")
        client.get("/v3/ping")

    # The record only ever holds one entry per version, however many
    # reloads happened.
    assert len(manager._high_water) <= 2


# --- concurrent reload + requests: no resurrection window ------------------

def test_concurrent_reload_and_requests_stay_monotonic():
    client, manager, clock = build(AFTER_SUNSET)
    assert client.get("/v2/ping").status_code == 410
    clock.set(BEFORE_DEPRECATION)

    violations = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            r = client.get("/v2/ping")
            if r.status_code != 410:
                violations.append(f"status {r.status_code}: {r.text}")
            elif r.json()["error"]["code"] != "version_retired":
                violations.append(f"bad body: {r.text}")

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for _ in range(100):
        reload_same_content(manager, clock)
    stop.set()
    for t in threads:
        t.join()
    assert violations == []


def test_concurrent_failed_reloads_keep_protection():
    client, manager, clock = build(AFTER_SUNSET)
    assert client.get("/v2/ping").status_code == 410
    clock.set(BEFORE_DEPRECATION)

    def bad_reloader():
        for _ in range(50):
            with pytest.raises(Exception):
                manager.reload_document({"versions": []})

    threads = [threading.Thread(target=bad_reloader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.get("/v2/ping").status_code == 410


# --- determinism including reloads -----------------------------------------

SCRIPT = [
    ("request", "/v3/ping"),
    ("request", "/v2/ping"),
    ("reload", None),
    ("request", "/v2/ping"),
    ("clock", BEFORE_DEPRECATION),
    ("request", "/v2/ping"),
    ("reload", None),
    ("request", "/v2/ping"),
    ("request", "/v3/ping"),
    ("reload_failed", None),
    ("request", "/v2/ping"),
]


def run_script():
    client, manager, clock = build(AFTER_SUNSET)
    out = []
    for action, arg in SCRIPT:
        if action == "request":
            r = client.get(arg)
            out.append(
                (
                    r.status_code,
                    r.json(),
                    {
                        k: v
                        for k, v in r.headers.items()
                        if k.lower() in ("deprecation", "sunset", "link", "x-api-version")
                    },
                )
            )
        elif action == "reload":
            reload_same_content(manager, clock)
        elif action == "reload_failed":
            with pytest.raises(Exception):
                manager.reload_document({"versions": []})
        elif action == "clock":
            clock.set(arg)
    return out


def test_request_reload_interleaving_is_deterministic():
    assert run_script() == run_script()
