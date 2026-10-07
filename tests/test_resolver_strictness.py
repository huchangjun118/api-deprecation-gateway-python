"""Tests for the strict deciding-source negotiation semantics.

* The highest-priority *present* source class decides the outcome alone.
* Malformed or conflicting values in lower-priority sources never fail a
  request that the deciding source can resolve.
* The request fails only when the deciding source itself is unparseable
  (``version_invalid``) or carries conflicting declarations
  (``version_conflict``) — the gateway never silently picks one.
"""

import pytest
from starlette.datastructures import Headers, QueryParams

from gateway.errors import VersionNegotiationError
from gateway.resolver import negotiate


# --- lower-priority garbage never blocks a decidable request -------------

def test_invalid_header_ignored_when_path_decides():
    result = negotiate("/v2/items", {"x-api-version": "abc"}, {})
    assert result.version == 2
    assert result.source == "path"


def test_invalid_query_ignored_when_path_decides():
    result = negotiate("/v2/items", {}, {"version": "latest"})
    assert result.version == 2
    assert result.source == "path"


def test_invalid_media_type_ignored_when_header_decides():
    result = negotiate(
        "/items",
        {"x-api-version": "2", "accept": "application/json; version=bogus"},
        {},
    )
    assert result.version == 2
    assert result.source == "header"


def test_invalid_query_ignored_when_media_type_decides():
    result = negotiate(
        "/items",
        {"accept": "application/json; version=3"},
        {"version": "1.5"},
    )
    assert result.version == 3
    assert result.source == "media_type"


def test_conflicting_lower_priority_source_ignored():
    # The Accept header carries two different versions, but the path
    # decides, so the request is still resolved.
    result = negotiate(
        "/v2/items",
        {"accept": "application/json; version=1, application/json; version=3"},
        {},
    )
    assert result.version == 2
    assert result.source == "path"


def test_malformed_lower_priority_source_not_listed():
    result = negotiate("/v2/items", {"x-api-version": "abc"}, {})
    assert [s.kind for s in result.sources] == ["path"]


# --- the deciding source's own failures are fatal -------------------------

def test_invalid_deciding_source_fails_even_if_lower_source_valid():
    # The header is the highest-priority present source; its invalid value
    # fails the request instead of falling through to the valid query param.
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {"x-api-version": "abc"}, {"version": "2"})
    assert exc.value.code == "version_invalid"
    assert exc.value.details["source"] == "header"


def test_empty_deciding_source_is_invalid():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {"x-api-version": "  "}, {"version": "2"})
    assert exc.value.code == "version_invalid"


# --- intra-source conflicts are rejected, never silently resolved ---------

def test_media_type_with_two_versions_conflicts():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate(
            "/items",
            {"accept": "application/json; version=1, application/json; version=2"},
            {},
        )
    assert exc.value.code == "version_conflict"
    assert exc.value.status_code == 400
    assert exc.value.details["source"] == "media_type"
    assert exc.value.details["versions"] == [1, 2]


def test_media_type_repeated_same_version_is_fine():
    result = negotiate(
        "/items",
        {"accept": "application/json; version=2, text/plain; version=2"},
        {},
    )
    assert result.version == 2
    assert result.source == "media_type"


def test_duplicate_query_params_conflict():
    params = QueryParams("version=1&version=2")
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, params)
    assert exc.value.code == "version_conflict"
    assert exc.value.details["source"] == "query"


def test_duplicate_query_params_same_value_is_fine():
    params = QueryParams("version=2&version=2")
    result = negotiate("/items", {}, params)
    assert result.version == 2
    assert result.source == "query"


def test_duplicate_headers_conflict():
    headers = Headers(raw=[(b"x-api-version", b"1"), (b"x-api-version", b"2")])
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", headers, {})
    assert exc.value.code == "version_conflict"
    assert exc.value.details["source"] == "header"


def test_unparseable_among_repeated_values_is_invalid_not_conflict():
    headers = Headers(raw=[(b"x-api-version", b"abc"), (b"x-api-version", b"2")])
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", headers, {})
    assert exc.value.code == "version_invalid"


# --- the three failure modes are mutually distinguishable -----------------

def test_failure_codes_are_distinct():
    with pytest.raises(VersionNegotiationError) as missing:
        negotiate("/items", {}, {})
    with pytest.raises(VersionNegotiationError) as invalid:
        negotiate("/items", {"x-api-version": "abc"}, {})
    with pytest.raises(VersionNegotiationError) as conflict:
        negotiate(
            "/items",
            {"accept": "application/json; version=1, application/json; version=2"},
            {},
        )
    codes = {missing.value.code, invalid.value.code, conflict.value.code}
    assert codes == {"version_required", "version_invalid", "version_conflict"}
