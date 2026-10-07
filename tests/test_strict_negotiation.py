"""Tests for strict source adjudication and distinct ambiguity error codes.

Covers:
* a malformed value in a lower-priority source never fails a request that a
  higher-priority source can already decide;
* conflicting versions *within one source* are rejected (never silently
  resolved by picking one);
* the three failure modes carry distinct machine-readable codes:
  ``version_required`` / ``version_invalid`` / ``version_conflict``.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.clock import FakeClock
from gateway.config import parse_policy
from gateway.errors import VersionNegotiationError
from gateway.manager import PolicyManager
from gateway.middleware import VersionGatewayMiddleware
from gateway.resolver import negotiate

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class MultiDict(dict):
    """A mapping that can carry repeated keys, like Starlette's QueryParams."""

    def __init__(self, pairs):
        super().__init__()
        self._pairs = list(pairs)
        for key, value in self._pairs:
            self.setdefault(key, value)

    def getlist(self, key):
        return [value for k, value in self._pairs if k == key]


def build_client():
    clock = FakeClock(NOW)
    policy = parse_policy(
        {
            "versions": [
                {"version": 2, "state": "available", "introduced_at": "2024-01-01T00:00:00Z"},
                {"version": 3, "state": "available", "introduced_at": "2025-01-01T00:00:00Z"},
            ]
        },
        now=clock.now(),
    )
    manager = PolicyManager(policy, clock=clock)
    app = FastAPI()
    app.add_middleware(VersionGatewayMiddleware, manager=manager)

    @app.get("/v{version}/ping")
    def ping(version: int):
        return {"version": version, "message": "pong"}

    @app.get("/ping")
    def ping_query():
        return {"message": "pong"}

    return TestClient(app)


# --- low-priority garbage must not block a decidable request --------------

def test_invalid_query_ignored_when_path_decides():
    result = negotiate("/v2/items", {}, {"version": "not-a-number"})
    assert result.version == 2
    assert result.source == "path"


def test_invalid_media_type_ignored_when_header_decides():
    result = negotiate(
        "/items",
        {"x-api-version": "3", "accept": "application/json; version=bogus"},
        {},
    )
    assert result.version == 3
    assert result.source == "header"


def test_invalid_query_ignored_when_media_type_decides():
    result = negotiate(
        "/items",
        {"accept": "application/json; version=2"},
        {"version": ""},
    )
    assert result.version == 2
    assert result.source == "media_type"


def test_conflicting_lower_source_ignored_when_path_decides():
    # Even a *conflict* in a lower-priority source must not fail the request.
    result = negotiate(
        "/v3/items",
        {"accept": "application/json; version=1, application/json; version=2"},
        {},
    )
    assert result.version == 3
    assert result.source == "path"


def test_invalid_lower_source_dropped_from_recorded_sources():
    result = negotiate("/v2/items", {}, {"version": "garbage"})
    assert [s.kind for s in result.sources] == ["path"]


# --- the deciding source's own failures are fatal -------------------------

def test_invalid_deciding_source_fails():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {"x-api-version": "abc"}, {"version": "2"})
    assert exc.value.code == "version_invalid"
    assert exc.value.details["source"] == "header"


def test_empty_deciding_source_fails():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, {"version": "  "})
    assert exc.value.code == "version_invalid"
    assert exc.value.details["source"] == "query"


# --- intra-source conflicts ------------------------------------------------

def test_media_type_conflict_rejected():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate(
            "/items",
            {"accept": "application/json; version=2, application/json; version=3"},
            {},
        )
    assert exc.value.code == "version_conflict"
    assert exc.value.details["source"] == "media_type"
    assert exc.value.details["values"] == ["2", "3"]


def test_media_type_conflict_not_silently_resolved_by_order():
    # Whichever version appears first, the outcome must be the same error.
    for accept in (
        "application/json; version=2, application/json; version=3",
        "application/json; version=3, application/json; version=2",
    ):
        with pytest.raises(VersionNegotiationError) as exc:
            negotiate("/items", {"accept": accept}, {})
        assert exc.value.code == "version_conflict"


def test_repeated_media_type_with_same_version_is_fine():
    result = negotiate(
        "/items",
        {"accept": "application/json; version=2, text/plain; version=2"},
        {},
    )
    assert result.version == 2
    assert result.source == "media_type"


def test_repeated_query_param_conflict_rejected():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, MultiDict([("version", "2"), ("version", "3")]))
    assert exc.value.code == "version_conflict"
    assert exc.value.details["source"] == "query"


def test_repeated_query_param_with_same_value_is_fine():
    result = negotiate("/items", {}, MultiDict([("version", "2"), ("version", "2")]))
    assert result.version == 2


def test_repeated_header_conflict_rejected():
    headers = MultiDict([("x-api-version", "2"), ("x-api-version", "3")])
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", headers, {})
    assert exc.value.code == "version_conflict"
    assert exc.value.details["source"] == "header"


# --- the three failure modes are mutually distinguishable ------------------

def test_failure_codes_are_distinct():
    codes = set()

    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, {})
    codes.add(exc.value.code)

    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, {"version": "x"})
    codes.add(exc.value.code)

    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {"accept": "application/json; version=2; q=0.5, application/json; version=3"}, {})
    codes.add(exc.value.code)

    assert codes == {"version_required", "version_invalid", "version_conflict"}


# --- end-to-end through the middleware ------------------------------------

def test_request_with_garbage_low_priority_source_is_served():
    client = build_client()
    r = client.get("/v2/ping", params={"version": "garbage"})
    assert r.status_code == 200
    assert r.json() == {"version": 2, "message": "pong"}
    assert r.headers["x-api-version"] == "2"
    assert r.headers["x-api-version-source"] == "path"


def test_request_with_conflicting_media_type_is_rejected():
    client = build_client()
    r = client.get(
        "/ping",
        headers={"accept": "application/json; version=2, application/json; version=3"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_conflict"


def test_request_with_repeated_conflicting_query_param_is_rejected():
    client = build_client()
    r = client.get("/ping?version=2&version=3")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_conflict"
