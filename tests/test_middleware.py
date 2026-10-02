"""Integration tests through the FastAPI app: rejection, headers, stats,
determinism and hot reload."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway.clock import FakeClock
from gateway.config import parse_policy
from gateway.manager import PolicyManager
from gateway.middleware import VersionGatewayMiddleware

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def policy_document(clock: FakeClock | None = None):
    """Policy document whose declared states are consistent with ``clock``."""
    now = (clock or FakeClock(NOW)).now()

    def state_for(deprecated_at, sunset_at):
        if sunset_at is not None and now >= sunset_at:
            return "offline"
        if deprecated_at is not None and now >= deprecated_at:
            return "deprecated"
        if sunset_at is not None:
            return "planned_offline"
        return "available"

    v1_dep = datetime(2023, 6, 1, tzinfo=timezone.utc)
    v1_sun = datetime(2024, 1, 1, tzinfo=timezone.utc)
    v2_dep = datetime(2026, 1, 1, tzinfo=timezone.utc)
    v2_sun = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return {
        "min_deprecation_window_days": 30,
        "versions": [
            {
                "version": 1,
                "state": state_for(v1_dep, v1_sun),
                "introduced_at": "2023-01-01T00:00:00Z",
                "deprecated_at": v1_dep.isoformat(),
                "sunset_at": v1_sun.isoformat(),
                "successor": 2,
            },
            {
                "version": 2,
                "state": state_for(v2_dep, v2_sun),
                "introduced_at": "2024-01-01T00:00:00Z",
                "deprecated_at": v2_dep.isoformat(),
                "sunset_at": v2_sun.isoformat(),
                "successor": 3,
            },
            {
                "version": 3,
                "state": "available",
                "introduced_at": "2025-01-01T00:00:00Z",
            },
        ],
    }


def build_app(clock: FakeClock | None = None):
    clock = clock or FakeClock(NOW)
    manager = PolicyManager(parse_policy(policy_document(clock), now=clock.now()), clock=clock)
    app = FastAPI()
    app.add_middleware(VersionGatewayMiddleware, manager=manager)
    app.state.gateway_manager = manager

    @app.get("/v{version}/ping")
    def ping(version: int):
        return {"version": version, "message": "pong"}

    @app.get("/ping")
    def ping_query():
        return {"message": "pong"}

    @app.get("/gateway/stats")
    def gateway_stats():
        return manager.stats.snapshot()

    @app.post("/gateway/reload")
    async def gateway_reload(request: Request):
        from fastapi.responses import JSONResponse

        body = await request.json()
        try:
            manager.reload_document(body)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
        return {"status": "reloaded"}

    return app, manager, clock


@pytest.fixture
def client():
    app, _, _ = build_app()
    return TestClient(app)


# --- offline rejection ---------------------------------------------------

def test_offline_version_rejected_via_path(client):
    r = client.get("/v1/ping")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"
    assert r.json()["error"]["details"]["successor"] == 2
    assert r.headers["Link"] == '</v2/>; rel="successor-version"'


def test_offline_version_rejected_via_header(client):
    r = client.get("/ping", headers={"x-api-version": "1"})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"


def test_offline_version_rejected_via_query(client):
    r = client.get("/ping", params={"version": "1"})
    assert r.status_code == 410


def test_offline_version_rejected_via_media_type(client):
    r = client.get("/ping", headers={"accept": "application/json; version=1"})
    assert r.status_code == 410


def test_offline_rejection_body_is_distinguishable(client):
    r = client.get("/v1/ping")
    body = r.json()
    assert body["error"]["code"] == "version_retired"
    assert "migrate" in body["error"]["message"]


# --- deprecated responses -------------------------------------------------

def test_deprecated_response_keeps_body_and_adds_headers(client):
    r = client.get("/v2/ping")
    assert r.status_code == 200
    # Body shape identical to an available version.
    assert r.json() == {"version": 2, "message": "pong"}
    assert r.headers["Deprecation"].startswith("@")
    assert "Sunset" in r.headers
    assert "GMT" in r.headers["Sunset"]
    assert r.headers["Link"] == '</v3/>; rel="successor-version"'


def test_available_response_has_no_deprecation_headers(client):
    r = client.get("/v3/ping")
    assert r.status_code == 200
    assert "Deprecation" not in r.headers
    assert "Sunset" not in r.headers


# --- negotiation errors ---------------------------------------------------

def test_missing_version_is_distinguishable(client):
    r = client.get("/ping")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_required"


def test_conflicting_versions_resolve_by_priority(client):
    # Path (higher priority) wins over header: version 2 is served with
    # deprecation headers, no error.
    r = client.get("/v2/ping", headers={"x-api-version": "3"})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert "Deprecation" in r.headers

    # Header wins over query: version 3 is served without deprecation headers.
    r = client.get("/ping", headers={"x-api-version": "3"}, params={"version": "2"})
    assert r.status_code == 200
    assert r.headers["x-api-version"] == "3"
    assert r.headers["x-api-version-source"] == "header"
    assert "Deprecation" not in r.headers


def test_unknown_version_is_distinguishable(client):
    r = client.get("/v9/ping")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_unknown"


# --- stats ----------------------------------------------------------------

def test_stats_count_per_version(client):
    client.get("/v3/ping")
    client.get("/v3/ping")
    client.get("/v2/ping")
    client.get("/v1/ping")
    counts = client.get("/gateway/stats").json()
    assert counts["versions"]["3"]["total"] == 2
    assert counts["versions"]["3"]["available"] == 2
    assert counts["versions"]["2"]["deprecated"] == 1
    assert counts["versions"]["1"]["offline"] == 1


# --- determinism ----------------------------------------------------------

REQUEST_SEQUENCE = [
    ("get", "/v3/ping", {}, None),
    ("get", "/v2/ping", {}, None),
    ("get", "/v1/ping", {}, None),
    ("get", "/ping", {"x-api-version": "3"}, None),
    ("get", "/ping", {}, {"version": "2"}),
    ("get", "/v2/ping", {"x-api-version": "3"}, None),
    ("get", "/v9/ping", {}, None),
    ("get", "/ping", {}, None),
]


def replay(clock):
    app, _, _ = build_app(clock)
    c = TestClient(app)
    out = []
    for method, path, headers, params in REQUEST_SEQUENCE:
        r = getattr(c, method)(path, headers=headers, params=params)
        out.append(
            (
                r.status_code,
                r.json(),
                {k: v for k, v in r.headers.items() if k.lower() in ("deprecation", "sunset", "link")},
            )
        )
    return out


def test_same_policy_and_clock_give_identical_decisions():
    clock1 = FakeClock(NOW)
    clock2 = FakeClock(NOW)
    first = replay(clock1)
    second = replay(clock2)
    assert first == second


def test_boundary_moment_determinism():
    # Exactly at deprecation instant -> deprecated headers.
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    app, _, _ = build_app(clock)
    c = TestClient(app)
    r = c.get("/v2/ping")
    assert r.status_code == 200
    assert "Deprecation" in r.headers

    # Exactly at sunset instant -> 410.
    clock.set(datetime(2026, 7, 1, tzinfo=timezone.utc))
    r = c.get("/v2/ping")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"


def test_clock_rollback_keeps_offline_rejected():
    clock = FakeClock(datetime(2026, 12, 1, tzinfo=timezone.utc))
    app, _, _ = build_app(clock)
    c = TestClient(app)
    assert c.get("/v2/ping").status_code == 410
    clock.set(datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert c.get("/v2/ping").status_code == 410


# --- hot reload via admin endpoint ----------------------------------------

def test_reload_endpoint_switches_policy(client):
    manager = client.app.state.gateway_manager
    new_doc = policy_document()
    new_doc["versions"] = [v for v in new_doc["versions"] if v["version"] != 1]
    r = client.post("/gateway/reload", json=new_doc)
    assert r.status_code == 200
    assert manager.current().get(1) is None
    # Version 1 now reports unknown instead of retired.
    r = client.get("/v1/ping")
    assert r.status_code == 404


def test_reload_endpoint_failure_keeps_active_policy(client):
    manager = client.app.state.gateway_manager
    r = client.post("/gateway/reload", json={"versions": []})
    assert r.status_code == 400
    assert manager.current().get(1) is not None
    assert client.get("/v1/ping").status_code == 410


# --- concurrent reload vs requests ----------------------------------------

def test_requests_during_reload_are_self_consistent(client):
    """Hammer the app with requests while reloads happen concurrently; every
    response must be internally consistent (version header matches the body
    and deprecation headers match the served version)."""
    import threading

    manager = client.app.state.gateway_manager
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            r = client.get("/v2/ping")
            if r.status_code == 200:
                body = r.json()
                if body.get("version") != 2:
                    errors.append(f"body version mismatch: {body}")
                if r.headers.get("x-api-version") != "2":
                    errors.append(f"header version mismatch: {r.headers.get('x-api-version')}")
                if "Deprecation" not in r.headers:
                    errors.append("missing Deprecation header on deprecated v2")
            elif r.status_code == 410:
                # Policy may have reloaded v2 to offline; that is also a
                # consistent outcome.
                if r.json()["error"]["code"] != "version_retired":
                    errors.append(f"unexpected 410 body: {r.json()}")
            else:
                errors.append(f"unexpected status {r.status_code}")

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for _ in range(100):
        # Reload the same valid policy; readers must never observe a torn state.
        manager.reload_document(policy_document())
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
