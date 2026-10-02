"""Tests for standard deprecation headers."""

from datetime import datetime, timezone

from gateway.headers import (
    deprecation_header,
    gateway_headers,
    sunset_header,
    successor_link,
)
from gateway.policy import LifecycleState, VersionConfig


def make_cfg(**overrides):
    cfg = VersionConfig(
        version=2,
        state=LifecycleState.DEPRECATED,
        introduced_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        deprecated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        sunset_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
        successor=3,
    )
    return cfg


def test_deprecation_header_is_at_timestamp():
    when = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert deprecation_header(when) == f"@{int(when.timestamp())}"


def test_sunset_header_is_http_date():
    when = datetime(2025, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert sunset_header(when) == "Tue, 01 Jul 2025 00:00:00 GMT"


def test_successor_link_format():
    assert successor_link(3) == '</v3/>; rel="successor-version"'


def test_available_version_has_no_headers():
    cfg = make_cfg(state=LifecycleState.AVAILABLE, deprecated_at=None, sunset_at=None)
    assert gateway_headers(cfg, LifecycleState.AVAILABLE) == {}


def test_deprecated_version_has_all_headers():
    headers = gateway_headers(make_cfg(), LifecycleState.DEPRECATED)
    assert "Deprecation" in headers
    assert headers["Deprecation"].startswith("@")
    assert "Sunset" in headers
    assert "GMT" in headers["Sunset"]
    assert headers["Link"] == '</v3/>; rel="successor-version"'


def test_planned_offline_has_sunset_warning_only():
    cfg = make_cfg(
        state=LifecycleState.PLANNED_OFFLINE,
        deprecated_at=None,
    )
    headers = gateway_headers(cfg, LifecycleState.PLANNED_OFFLINE)
    assert "Deprecation" not in headers
    assert "Sunset" in headers
    assert "Link" not in headers


def test_offline_version_gets_no_body_headers():
    # Offline requests are rejected with 410 before headers are attached;
    # gateway_headers itself must not emit Deprecation for offline state.
    headers = gateway_headers(make_cfg(), LifecycleState.OFFLINE)
    assert headers == {}
