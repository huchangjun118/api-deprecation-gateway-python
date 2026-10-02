"""Standard deprecation response headers.

* ``Deprecation`` (RFC 8594) announces the deprecation instant. The
  canonical value is an HTTP-date; the ``@<http-date>`` form is also
  emitted for clients following that convention.
* ``Sunset`` (RFC 8594) announces the instant at which the version goes
  offline. It is only sent when ``sunset_at`` is known.
* ``Link`` with ``rel="successor-version"`` (and the policy-supplied
  ``deprecation`` relation, when a URL template exists) tells clients
  where to migrate. The successor version id is always also announced
  via ``X-Api-Successor-Version`` so machine readers need not parse
  ``Link``.

Only headers are ever produced here; the response body is left exactly
as the application generated it.
"""

from __future__ import annotations

from email.utils import format_datetime
from urllib.parse import quote

from .models import Policy, VersionInfo

SUCCESSOR_HEADER = "X-Api-Successor-Version"


def http_date(instant) -> str:
    """Format a timezone-aware datetime as an RFC 7231 IMF-fixdate (GMT)."""
    return format_datetime(instant, usegmt=True)


def _successor_url(info: VersionInfo, policy: Policy,
                   request_path: str) -> str | None:
    if info.successor is None:
        return None
    if policy.version_url_template is not None:
        return policy.version_url_template.replace(
            "{version}", quote(info.successor, safe=""))
    # Best-effort: rewrite the version segment in the request path, so
    # the pointer is usable even without an explicit URL template.
    import re

    match = re.search(policy.version_path_regex, request_path)
    if match is not None:
        start, end = match.span("version")
        return (request_path[:start] + quote(info.successor, safe="")
                + request_path[end:])
    return None


def _link_values(info: VersionInfo, policy: Policy,
                 request_path: str) -> list[str]:
    values: list[str] = []
    url = _successor_url(info, policy, request_path)
    if url is not None:
        values.append(f'<{url}>; rel="successor-version"; anchor="#version-{info.successor}"')
    elif info.successor is not None:
        # No URL can be derived; still identify the successor version.
        values.append(f'<urn:api-version:{quote(info.successor, safe="")}>'
                      '; rel="successor-version"')
    return values


def build_deprecation_headers(info: VersionInfo, now,
                              policy: Policy, request_path: str) -> dict[str, str]:
    """Headers attached to responses served by a deprecated version."""
    headers: dict[str, str] = {}
    if info.deprecated_at is not None:
        stamp = http_date(info.deprecated_at)
        headers["Deprecation"] = f"@{stamp}"
        headers["X-Deprecation-Date"] = stamp
    if info.sunset_at is not None:
        headers["Sunset"] = http_date(info.sunset_at)
    if info.successor is not None:
        headers[SUCCESSOR_HEADER] = info.successor
    links = _link_values(info, policy, request_path)
    if links:
        headers["Link"] = ", ".join(links)
    return headers


def build_retirement_headers(info: VersionInfo, now,
                             policy: Policy, request_path: str) -> dict[str, str]:
    """Headers attached when a request is rejected because the version is gone."""
    headers: dict[str, str] = {}
    if info.deprecated_at is not None:
        headers["Deprecation"] = f"@{http_date(info.deprecated_at)}"
    # Sunset is the moment the version became/will become unavailable.
    if info.sunset_at is not None:
        headers["Sunset"] = http_date(info.sunset_at)
    if info.successor is not None:
        headers[SUCCESSOR_HEADER] = info.successor
    links = _link_values(info, policy, request_path)
    if links:
        headers["Link"] = ", ".join(links)
    return headers
