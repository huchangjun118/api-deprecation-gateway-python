"""Tests for the wired application (main.py): existing behavior is preserved
and gateway endpoints work end-to-end."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_root_unchanged():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}
    assert "Deprecation" not in r.headers


def test_versioned_endpoints_still_work():
    r = client.get("/v3/ping")
    assert r.status_code == 200
    assert r.json() == {"version": 3, "message": "pong"}


def test_gateway_versions_endpoint():
    r = client.get("/gateway/versions")
    assert r.status_code == 200
    versions = {v["version"]: v["state"] for v in r.json()["versions"]}
    assert versions[1] == "offline"
    assert versions[2] == "deprecated"
    assert versions[3] == "available"


def test_gateway_stats_endpoint():
    r = client.get("/gateway/stats")
    assert r.status_code == 200
    assert "versions" in r.json()


def test_reload_without_source_returns_distinguishable_error():
    r = client.post("/gateway/reload")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_policy_source"
