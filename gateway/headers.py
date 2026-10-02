"""Standard deprecation / sunset / successor response headers.

* ``Deprecation`` (RFC 8594) — ``@<unix-timestamp>`` marking when the version
  was deprecated.
* ``Sunset`` (RFC 8594, HTTP-date per RFC 7231) — when the version is removed.
* ``Link`` with ``rel="successor-version"`` (RFC 5829) — where to migrate.

Response bodies are never altered: clients that ignore headers see the same
payload shape as available versions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Iterable

from .policy import LifecycleState, VersionConfig

__all__ = [
    "deprecation_header",
    "sunset_header",
    "successor_link",
    "gateway_headers",
    "build_link_header",
]


def deprecation_header(when: datetime) -> str:
    """RFC 8594 ``Deprecation`` value: ``@<unix-seconds>``."""
    return f"@{int(when.timestamp())}"


def sunset_header(when: datetime) -> str:
    """RFC 8594 ``Sunset`` value: an IMF-fixdate HTTP date."""
    return format_datetime(when.astimezone(timezone.utc), usegmt=True)


def successor_link(successor: int, *, base_path: str = "") -> str:
    """RFC 5829 ``Link`` value pointing at the successor version."""
    target = f"{base_path}/v{successor}/"
    return f'<{target}>; rel="successor-version"'


def build_link_header(links: Iterable[tuple[str, str]]) -> str:
    """Combine (target, rel) pairs into a single ``Link`` header value."""
    return ", ".join(f'<{target}>; rel="{rel}"' for target, rel in links)


def gateway_headers(
    cfg: VersionConfig,
    state: LifecycleState,
    *,
    base_path: str = "",
) -> dict[str, str]:
    """Build the gateway headers for a response of a version in ``state``.

    Returns an empty dict for available versions.  The body is left untouched.
    """
    headers: dict[str, str] = {}

    if state is LifecycleState.DEPRECATED:
        if cfg.deprecated_at is not None:
            headers["Deprecation"] = deprecation_header(cfg.deprecated_at)
        if cfg.sunset_at is not None:
            headers["Sunset"] = sunset_header(cfg.sunset_at)
    elif state is LifecycleState.PLANNED_OFFLINE:
        # Still usable, but give clients an early warning of the planned
        # removal date.
        if cfg.sunset_at is not None:
            headers["Sunset"] = sunset_header(cfg.sunset_at)

    links: list[tuple[str, str]] = []
    if cfg.successor is not None and state is LifecycleState.DEPRECATED:
        links.append((f"{base_path}/v{cfg.successor}/", "successor-version"))
    if links:
        headers["Link"] = build_link_header(links)

    return headers
