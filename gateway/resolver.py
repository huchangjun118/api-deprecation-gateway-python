"""Version negotiation from multiple request sources.

Supported sources, in fixed priority order (highest first):

1. **URL path** — ``/v1/...`` or ``/version/1/...``
2. **Request header** — ``X-API-Version: 1``
3. **Media type parameter** — ``Accept: application/json; version=1``
4. **Query parameter** — ``?version=1``

Rules:

* every present source must parse to an integer version;
* when several sources are present they are resolved by the fixed priority
  above: the highest-priority source wins, deterministically;
* when no source is present on a versioned path the request is rejected with
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


def _parse_version(raw: str, *, kind: str) -> int:
    text = raw.strip()
    if not text:
        raise VersionNegotiationError(
            f"version source '{kind}' is present but empty",
            code="version_invalid",
            status_code=400,
            details={"source": kind, "raw": raw},
        )
    if not re.fullmatch(r"\d+", text):
        raise VersionNegotiationError(
            f"version source '{kind}' has non-numeric value {raw!r}",
            code="version_invalid",
            status_code=400,
            details={"source": kind, "raw": raw},
        )
    return int(text)


def _extract_path(path: str) -> VersionSource | None:
    match = _PATH_RE.match(path)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    return VersionSource(kind="path", raw=raw, version=int(raw))


def _extract_header(headers: Mapping[str, str]) -> VersionSource | None:
    raw = headers.get(_HEADER_NAME)
    if raw is None:
        return None
    return VersionSource(kind="header", raw=raw, version=_parse_version(raw, kind="header"))


def _extract_query(query_params: Mapping[str, str]) -> VersionSource | None:
    raw = query_params.get(_QUERY_KEY)
    if raw is None:
        return None
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
    accept = headers.get("accept")
    if not accept:
        return None
    for key, raw in _iter_accept_params(accept):
        if key == _MEDIA_PARAM:
            return VersionSource(
                kind="media_type",
                raw=raw,
                version=_parse_version(raw, kind="media_type"),
            )
    return None


def negotiate(
    path: str,
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
) -> NegotiationResult:
    """Negotiate a single version from the request parts.

    Raises :class:`VersionNegotiationError` (``version_required`` /
    ``version_invalid`` / ``version_conflict``) instead of guessing.
    """
    candidates: list[VersionSource] = []

    path_source = _extract_path(path)
    if path_source is not None:
        candidates.append(path_source)
    header_source = _extract_header(headers)
    if header_source is not None:
        candidates.append(header_source)
    media_source = _extract_media_type(headers)
    if media_source is not None:
        candidates.append(media_source)
    query_source = _extract_query(query_params)
    if query_source is not None:
        candidates.append(query_source)

    if not candidates:
        raise VersionNegotiationError(
            "no API version specified: provide one via URL path (/vN/...), "
            "the X-API-Version header, an Accept media type parameter, or ?version=",
            code="version_required",
            status_code=400,
        )

    # Deterministic priority breaks ties: the highest-priority source wins.
    ordered = sorted(candidates, key=lambda c: SOURCE_PRIORITY.index(c.kind))
    winner = ordered[0]
    return NegotiationResult(version=winner.version, sources=tuple(ordered))
