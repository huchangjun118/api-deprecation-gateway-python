"""Deprecation/Sunset/Link header formatting."""

from __future__ import annotations

from version_gateway import Policy, VersionInfo, VersionState
from version_gateway.headers import (
    SUCCESSOR_HEADER,
    build_deprecation_headers,
    build_retirement_headers,
    http_date,
)

from .conftest import DEP, SUN, T0


def _v1(**kwargs):
    params = dict(version="1", state=VersionState.DEPRECATED,
                  introduced_at=T0, deprecated_at=DEP, sunset_at=SUN,
                  successor="2")
    params.update(kwargs)
    return VersionInfo(**params)


def test_http_date_is_rfc7231_gmt():
    assert http_date(SUN) == "Tue, 03 Mar 2026 00:00:00 GMT"


def test_deprecated_headers_contain_all_standard_fields():
    policy = Policy(versions={"1": _v1()},
                    version_url_template="https://api/v{version}/")
    headers = build_deprecation_headers(_v1(), DEP, policy, "/items")
    assert headers["Deprecation"] == "@Sun, 01 Feb 2026 00:00:00 GMT"
    assert headers["Sunset"] == "Tue, 03 Mar 2026 00:00:00 GMT"
    assert headers[SUCCESSOR_HEADER] == "2"
    assert headers["Link"] == (
        '<https://api/v2/>; rel="successor-version"; anchor="#version-2"')


def test_no_sunset_header_when_not_announced():
    info = _v1(sunset_at=None)
    headers = build_deprecation_headers(info, DEP,
                                        Policy(versions={"1": info}), "/items")
    assert "Sunset" not in headers
    assert "Deprecation" in headers


def test_path_rewrite_successor_url_without_template():
    policy = Policy(versions={"1": _v1()})
    headers = build_retirement_headers(_v1(), SUN, policy, "/v1/items")
    assert "/v2/items" in headers["Link"]


def test_retirement_headers_identify_successor():
    policy = Policy(versions={"1": _v1()})
    headers = build_retirement_headers(_v1(), SUN, policy, "/items")
    assert headers[SUCCESSOR_HEADER] == "2"
    assert "Sunset" in headers and "Deprecation" in headers
