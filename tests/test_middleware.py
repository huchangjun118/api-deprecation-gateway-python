"""HTTP-level behaviour via Starlette/FastAPI TestClient."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from version_gateway import (
    FixedClock,
    PolicyStore,
    VersionMetrics,
    VersionState,
)
from version_gateway.middleware import install

from .conftest import DEP, SUN, T0, make_policy


@pytest.fixture
def client(clock):
    store = PolicyStore(make_policy(), clock=clock)
    metrics = VersionMetrics()
    app = FastAPI()
    install(app, store, clock=clock, metrics=metrics)

    @app.get("/items")
    def items():
        return {"items": ["x"], "count": 1}

    @app.get("/v1/items")
    def items_v1():
        return {"items": ["x"], "count": 1, "v": 1}

    return TestClient(app), store, metrics, clock


def test_available_version_body_unchanged_no_deprecation_headers(client):
    http, *_ = client
    response = http.get("/items")
    assert response.status_code == 200
    assert response.json() == {"items": ["x"], "count": 1}
    assert "Deprecation" not in response.headers
    assert "Sunset" not in response.headers


def test_deprecated_version_adds_headers_but_keeps_body(client):
    http, _, _, clock = client
    clock.set(DEP)
    response = http.get("/items", headers={"X-Api-Version": "1"})
    assert response.status_code == 200
    assert response.json() == {"items": ["x"], "count": 1}
    assert response.headers["Deprecation"].startswith("@")
    assert response.headers["Sunset"] == "Tue, 03 Mar 2026 00:00:00 GMT"
    assert response.headers["X-Api-Successor-Version"] == "2"
    link = response.headers["Link"]
    assert 'rel="successor-version"' in link
    assert "https://api.example.com/v2/" in link


def test_path_version_deprecated(client):
    http, _, _, clock = client
    clock.set(DEP)
    response = http.get("/v1/items")
    assert response.status_code == 200
    assert response.json()["v"] == 1
    assert "Sunset" in response.headers


def test_retired_version_rejected_with_guidance_from_any_source(client):
    http, _, _, clock = client
    clock.set(SUN)
    requests = (
        ("GET", "/v1/items", None, None),
        ("GET", "/items", {"X-Api-Version": "1"}, None),
        ("GET", "/items", None, {"api_version": "1"}),
        ("GET", "/items", {"Accept": "application/json; version=1"}, None),
    )
    for method, path, headers, params in requests:
        response = http.request(method, path, headers=headers, params=params)
        assert response.status_code == 410, (method, path, response.status_code)
        body = response.json()["error"]
        assert body["code"] == "version_retired"
        assert body["successor_version"] == "2"
        assert body["successor_url"] == "https://api.example.com/v2/"
        assert response.headers["X-Api-Successor-Version"] == "2"
        assert "Sunset" in response.headers


def test_unknown_and_malformed_and_ambiguous_distinguishable(client):
    http, _, _, clock = client
    clock.set(DEP)

    resp = http.get("/items", headers={"X-Api-Version": "77"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_version"

    resp = http.get("/items", headers={"X-Api-Version": " "})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "malformed_version"

    resp = http.get("/items", headers={
        "Accept": "application/json;version=1, application/json;version=2"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ambiguous_version"


def test_clock_rollback_keeps_retirement_over_http(client):
    http, _, _, clock = client
    clock.set(SUN)
    assert http.get("/v1/items").status_code == 410
    clock.set(T0)
    assert http.get("/v1/items").status_code == 410


def test_metrics_recorded_per_version(client):
    http, _, metrics, clock = client
    clock.set(DEP)
    http.get("/items")  # default v2
    http.get("/items", headers={"X-Api-Version": "1"})
    http.get("/items", headers={"X-Api-Version": "1"})
    snap = metrics.snapshot()
    assert snap["2"]["available"] == 1
    assert snap["1"]["planned-retirement"] == 2
    assert metrics.deprecated_versions() == {"1": 2}


def test_hot_reload_takes_effect_without_restart(client):
    http, store, metrics, clock = client
    clock.set(DEP)
    # Before reload: v1 deprecated but served.
    assert http.get("/items", headers={"X-Api-Version": "1"}).status_code == 200
    # Reload declares v1 retired right away.
    from version_gateway import VersionInfo
    retired_policy = make_policy(versions={
        "1": VersionInfo("1", VersionState.RETIRED, T0, DEP, SUN, successor="2"),
        "2": make_policy().versions["2"],
    })
    store.reload(retired_policy)
    response = http.get("/items", headers={"X-Api-Version": "1"})
    assert response.status_code == 410
    assert response.json()["error"]["successor_version"] == "2"


def test_failed_reload_does_not_change_serving(client):
    http, store, _, clock = client
    clock.set(DEP)
    from version_gateway import Policy
    from version_gateway.errors import PolicyValidationError
    with pytest.raises(PolicyValidationError):
        store.reload(Policy(versions={}))
    # Existing policy still serves traffic.
    assert http.get("/items").status_code == 200
