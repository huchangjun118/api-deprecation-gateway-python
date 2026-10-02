"""Version-source extraction, precedence and distinguishable errors."""

from __future__ import annotations

import pytest

from version_gateway.errors import VersionNegotiationError
from version_gateway.negotiation import (
    SOURCE_HEADER,
    SOURCE_MEDIA_TYPE,
    SOURCE_PATH,
    SOURCE_QUERY,
    RequestView,
    extract_version,
    parse_accept_versions,
)

KW = dict(
    header_name="X-Api-Version",
    query_param="api_version",
    accept_version_param="version",
    version_path_regex=r"/v(?P<version>\d+)",
)


def req(path="/", headers=None, query=None):
    return RequestView("GET", path, headers or {}, query or {})


def test_no_source_returns_none():
    assert extract_version(req(), **KW) is None


@pytest.mark.parametrize("path,expected", [
    ("/v1/items", "1"),
    ("/api/v42/details", "42"),
    ("/items", None),
])
def test_path_source(path, expected):
    result = extract_version(req(path=path), **KW)
    assert (result.value if result else None) == expected


def test_header_source_case_insensitive():
    assert extract_version(req(headers={"x-api-version": "3"}), **KW
                           ).value == "3"


def test_query_source():
    assert extract_version(req(query={"api_version": "4"}), **KW
                           ).value == "4"


def test_media_type_source_plain_and_quoted():
    result = extract_version(
        req(headers={"Accept": 'application/json; version="5"'}), **KW)
    assert result.value == "5" and result.source == SOURCE_MEDIA_TYPE


def test_media_type_repeated_same_value_is_not_ambiguous():
    result = extract_version(
        req(headers={"Accept":
                     "application/json;version=5, application/*; version=5"}),
        **KW)
    assert result.value == "5"


def test_precedence_path_beats_everything():
    request = req(
        path="/v2/items",
        headers={"X-Api-Version": "3",
                 "Accept": "application/json; version=5"},
        query={"api_version": "4"},
    )
    decision = extract_version(request, **KW)
    assert (decision.value, decision.source) == ("2", SOURCE_PATH)


def test_precedence_header_beats_query_and_media_type():
    request = req(headers={"X-Api-Version": "3",
                           "Accept": "application/json; version=5"},
                  query={"api_version": "4"})
    decision = extract_version(request, **KW)
    assert (decision.value, decision.source) == ("3", SOURCE_HEADER)


def test_precedence_query_beats_media_type():
    decision = extract_version(
        req(headers={"Accept": "application/json; version=5"},
            query={"api_version": "4"}), **KW)
    assert (decision.value, decision.source) == ("4", SOURCE_QUERY)


def test_conflicting_sources_do_not_error_highest_wins():
    # Conflicting values across sources are resolved by precedence, never
    # guessed and never treated as an error.
    decision = extract_version(
        req(path="/v2/x", headers={"X-Api-Version": "9"}), **KW)
    assert decision.value == "2"


def test_ambiguous_media_type_values_is_distinguishable():
    with pytest.raises(VersionNegotiationError) as exc:
        extract_version(
            req(headers={"Accept":
                         "application/json;version=5, application/json;version=6"}),
            **KW)
    assert exc.value.error_code == "ambiguous_version"
    assert exc.value.status_code == 400


@pytest.mark.parametrize("headers", [
    {"X-Api-Version": "   "},
])
def test_blank_explicit_header_is_malformed(headers):
    with pytest.raises(VersionNegotiationError) as exc:
        extract_version(req(headers=headers), **KW)
    assert exc.value.error_code == "malformed_version"


def test_blank_query_is_malformed():
    with pytest.raises(VersionNegotiationError) as exc:
        extract_version(req(query={"api_version": ""}), **KW)
    assert exc.value.error_code == "malformed_version"


def test_parse_accept_versions_helpers():
    assert parse_accept_versions(
        "text/html, application/json; version=1; charset=utf-8", "version") == ["1"]
