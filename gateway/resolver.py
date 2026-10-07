"""Version negotiation from multiple request sources.

Supported sources, in fixed priority order (highest first):

1. **URL path** — ``/v1/...`` or ``/version/1/...``
2. **Request header** — ``X-API-Version: 1``
3. **Media type parameter** — ``Accept: application/json; version=1``
4. **Query parameter** — ``?version=1``

Adjudication rules:

* the highest-priority source that is *present* decides the version;
  lower-priority sources are then ignored for the decision — a malformed
  value in a lower-priority source never fails an otherwise decidable
  request;
* the deciding source must itself parse to exactly one integer version:
  an unparseable value yields ``version_invalid`` and several conflicting
  values *within the same source* (e.g. two different ``version=`` media
  type parameters, or a repeated header / query parameter) yield
  ``version_conflict`` — the gateway never silently picks one;
* when no source is present at all the request is rejected with
  ``version_required`` rather than falling back to an implicit default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import VersionNegotiationError

__all__ = ["VersionSource", "NegotiationResult", "negotiate", "SOURCE_PRIORITY"]

SOURCE_PRIORITY = ("path", "header", "media_type", "query")

_PATH_RE = re.compile(r"^/(?:v(\d+)|version/(\d+))(?=/|$)", re.IGNORECASE)
_HEADER_NAME = "x-api-version"
_QUERY_KEY = "version"
_MEDIA_PARAM = "version"


@dataclass(frozen=True)
class VersionSource:
    """A single source that expressed a version."""

    kind: str  # one of SOURCE_PRIORITY
    raw: str
    version: int


@dataclass(frozen=True)
class NegotiationResult:
    """Outcome of negotiation: exactly one version plus the supporting sources."""

    version: int
    sources: tuple[VersionSource, ...]

    @property
    def source(self) -> str:
        """The highest-priority source that determined the version."""
        return self.sources[0].kind


def _invalid(kind: str, raw: str, reason: str) -> VersionNegotiationError:
    return VersionNegotiationError(
        f"version source '{kind}' {reason}",
        code="version_invalid",
        status_code=400,
        details={"source": kind, "raw": raw},
    )


def _conflict(kind: str, raws: Iterable[str]) -> VersionNegotiationError:
    values = sorted(set(raws))
    return VersionNegotiationError(
        f"version source '{kind}' carries conflicting versions: "
        + ", ".join(repr(v) for v in values),
        code="version_conflict",
        status_code=400,
        details={"source": kind, "values": values},
    )


def _parse_version(raw: str, *, kind: str) -> int:
    text = raw.strip()
    if not text:
        raise _invalid(kind, raw, "is present but empty")
    if not re.fullmatch(r"\d+", text):
        raise _invalid(kind, raw, f"has non-numeric value {raw!r}")
    return int(text)


def _get_all(mapping: Mapping[str, str], key: str) -> list[str]:
    """Return every value bound to ``key`` (multi-value aware)."""
    getlist = getattr(mapping, "getlist", None)
    if getlist is not None:
        return list(getlist(key))
    value = mapping.get(key)
    return [] if value is None else [value]


def _extract_path(path: str) -> VersionSource | None:
    match = _PATH_RE.match(path)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    return VersionSource(kind="path", raw=raw, version=int(raw))


def _extract_header(headers: Mapping[str, str]) -> VersionSource | None:
    raws = _get_all(headers, _HEADER_NAME)
    if not raws:
        return None
    distinct = {raw.strip() for raw in raws}
    if len(distinct) > 1:
        raise _conflict("header", distinct)
    raw = raws[0]
    return VersionSource(kind="header", raw=raw, version=_parse_version(raw, kind="header"))


def _extract_query(query_params: Mapping[str, str]) -> VersionSource | None:
    raws = _get_all(query_params, _QUERY_KEY)
    if not raws:
        return None
    distinct = {raw.strip() for raw in raws}
    if len(distinct) > 1:
        raise _conflict("query", distinct)
    raw = raws[0]
    return VersionSource(kind="query", raw=raw, version=_parse_version(raw, kind="query"))


def _iter_accept_params(accept: str) -> Iterable[tuple[str, str]]:
    """Yield (parameter, value) pairs from an Accept header value."""
    for media_range in accept.split(","):
        parts = [p.strip() for p in media_range.split(";")]
        for param in parts[1:]:
            if "=" not in param:
                continue
            key, value = param.split("=", 1)
            yield key.strip().lower(), value.strip().strip('"')


def _extract_media_type(headers: Mapping[str, str]) -> VersionSource | None:
    raws: list[str] = []
    for accept in _get_all(headers, "accept"):
        for key, raw in _iter_accept_params(accept):
            if key == _MEDIA_PARAM:
                raws.append(raw)
    if not raws:
        return None
    distinct = {raw.strip() for raw in raws}
    if len(distinct) > 1:
        raise _conflict("media_type", distinct)
    raw = raws[0]
    return VersionSource(
        kind="media_type",
        raw=raw,
        version=_parse_version(raw, kind="media_type"),
    )


def negotiate(
    path: str,
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
) -> NegotiationResult:
    """Negotiate a single version from the request parts.

    Raises :class:`VersionNegotiationError` (``version_required`` /
    ``version_invalid`` / ``version_conflict``) instead of guessing.
    """
    # Extract every present source, deferring parse failures: a broken
    # low-priority source must not fail a request a higher-priority source
    # can already decide.
    outcomes: list[tuple[str, VersionSource | VersionNegotiationError]] = []
    for kind, extract in (
        ("path", lambda: _extract_path(path)),
        ("header", lambda: _extract_header(headers)),
        ("media_type", lambda: _extract_media_type(headers)),
        ("query", lambda: _extract_query(query_params)),
    ):
        try:
            source = extract()
        except VersionNegotiationError as error:
            outcomes.append((kind, error))
        else:
            if source is not None:
                outcomes.append((kind, source))

    if not outcomes:
        raise VersionNegotiationError(
            "no API version specified: provide one via URL path (/vN/...), "
            "the X-API-Version header, an Accept media type parameter, or ?version=",
            code="version_required",
            status_code=400,
        )

    # The highest-priority present source decides; outcomes are already in
    # priority order.  Its own failure (invalid / conflicting) is fatal.
    deciding_kind, deciding = outcomes[0]
    if isinstance(deciding, VersionNegotiationError):
        raise deciding

    # Keep the remaining *valid* sources for observability, in priority
    # order; broken lower-priority sources are dropped from the record.
    supporting = [deciding] + [
        outcome
        for _, outcome in outcomes[1:]
        if isinstance(outcome, VersionSource)
    ]
    return NegotiationResult(version=deciding.version, sources=tuple(supporting))
