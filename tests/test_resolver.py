"""Tests for multi-source version negotiation and priority."""

import pytest

from gateway.errors import VersionNegotiationError
from gateway.resolver import SOURCE_PRIORITY, negotiate


def test_path_version():
    result = negotiate("/v2/items", {}, {})
    assert result.version == 2
    assert result.source == "path"


def test_path_version_style():
    result = negotiate("/version/3/items", {}, {})
    assert result.version == 3


def test_header_version():
    result = negotiate("/items", {"x-api-version": "2"}, {})
    assert result.version == 2
    assert result.source == "header"


def test_media_type_version():
    result = negotiate("/items", {"accept": "application/json; version=2"}, {})
    assert result.version == 2
    assert result.source == "media_type"


def test_media_type_version_with_multiple_ranges():
    result = negotiate(
        "/items",
        {"accept": "text/plain; q=0.5, application/json; version=4; q=0.9"},
        {},
    )
    assert result.version == 4


def test_query_version():
    result = negotiate("/items", {}, {"version": "2"})
    assert result.version == 2
    assert result.source == "query"


def test_no_source_raises_version_required():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, {})
    assert exc.value.code == "version_required"
    assert exc.value.status_code == 400


def test_path_beats_header():
    result = negotiate("/v1/items", {"x-api-version": "2"}, {})
    assert result.version == 1
    assert result.source == "path"


def test_header_beats_media_type():
    result = negotiate(
        "/items",
        {"x-api-version": "2", "accept": "application/json; version=3"},
        {},
    )
    assert result.version == 2
    assert result.source == "header"


def test_media_type_beats_query():
    result = negotiate(
        "/items",
        {"accept": "application/json; version=2"},
        {"version": "3"},
    )
    assert result.version == 2
    assert result.source == "media_type"


def test_all_sources_agree():
    result = negotiate(
        "/v2/items",
        {"x-api-version": "2", "accept": "application/json; version=2"},
        {"version": "2"},
    )
    assert result.version == 2
    assert len(result.sources) == 4
    assert [s.kind for s in result.sources] == list(SOURCE_PRIORITY)


def test_conflicting_sources_resolved_by_priority():
    # Path (highest priority) wins over header, deterministically.
    result = negotiate("/v1/items", {"x-api-version": "2"}, {})
    assert result.version == 1
    assert result.source == "path"
    # All present sources are recorded for observability.
    assert {s.kind for s in result.sources} == {"path", "header"}


def test_conflict_header_vs_query():
    result = negotiate("/items", {"x-api-version": "2"}, {"version": "3"})
    assert result.version == 2
    assert result.source == "header"


def test_same_input_gives_same_winner():
    first = negotiate("/v1/items", {"x-api-version": "2"}, {"version": "3"})
    second = negotiate("/v1/items", {"x-api-version": "2"}, {"version": "3"})
    assert first.version == second.version == 1
    assert first.source == second.source == "path"


def test_non_numeric_header_is_invalid():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {"x-api-version": "abc"}, {})
    assert exc.value.code == "version_invalid"
    assert exc.value.status_code == 400


def test_non_numeric_query_is_invalid():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {}, {"version": "1.5"})
    assert exc.value.code == "version_invalid"


def test_non_numeric_media_type_is_invalid():
    with pytest.raises(VersionNegotiationError) as exc:
        negotiate("/items", {"accept": "application/json; version=latest"}, {})
    assert exc.value.code == "version_invalid"


def test_path_does_not_match_unrelated_prefix():
    with pytest.raises(VersionNegotiationError):
        negotiate("/versions/2/items", {}, {})


def test_priority_order_is_documented():
    assert SOURCE_PRIORITY == ("path", "header", "media_type", "query")
