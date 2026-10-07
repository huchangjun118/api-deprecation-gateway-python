"""Version negotiation from multiple request sources.

Supported sources, in fixed priority order (highest first):

1. **URL path** — ``/v1/...`` or ``/version/1/...``
2. **Request header** — ``X-API-Version: 1``
3. **Media type parameter** — ``Accept: application/json; version=1``
4. **Query parameter** — ``?version=1``

Rules:

* sources are examined in the fixed priority order above; the
  highest-priority source class that is *present* is the **deciding
  source** and alone determines the outcome;
* if the deciding source parses to exactly one version, that version wins
  and lower-priority sources are ignored — even when a lower-priority
  source carries an unparseable or conflicting value, the request is
  still resolved by the deciding source;
* the request is rejected only when the deciding source itself cannot
  yield a unique version:

  * its value cannot be parsed -> ``version_invalid``;
  * it carries several conflicting version declarations (e.g. two
    different ``version=`` media type parameters in one ``Accept``
    header, or repeated ``X-API-Version`` / ``?version=`` values that
    disagree) -> ``version_conflict``; the gateway never silently picks
    one of the conflicting declarations;
* when no source is present on a versioned path the request is rejected
  with ``version_required`` rather than falling back to an implicit
  default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

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


@dataclass(frozen=True)
class _SourceOutcome:
    """Raw extraction result of one source class (never raises)."""

    kind: str
    raws: tuple[str, ...]  # every declaration found in this source class

    @property
    def parsed(self) -> tuple[int, ...] | None:
        """All declarations parsed, or ``None`` if any is unparseable."""
        versions: list[int] = []
        for raw in self.raws:
            text = raw.strip()
            if not text or not re.fullmatch(r"\d+", text):
                return None
            versions.append(int(text))
        return tuple(versions)

    @property
    def distinct_versions(self) -> tuple[int, ...]:
        parsed = self.parsed
        if parsed is None:
            return ()
        return tuple(sorted(set(parsed)))


def _invalid_error(kind: str, raw: str) -> VersionNegotiationError:
    text = raw.strip()
    if not text:
        message = f"version source '{kind}' is present but empty"
    else:
        message = f"version source '{kind}' has non-numeric value {raw!r}"
    return VersionNegotiationError(
        message,
        code="version_invalid",
        status_code=400,
        details={"source": kind, "raw": raw},
    )


def _conflict_error(kind: str, raws: Sequence[str], versions: Sequence[int]) -> VersionNegotiationError:
    return VersionNegotiationError(
        f"version source '{kind}' carries conflicting versions: "
        + ", ".join(str(v) for v in versions),
        code="version_conflict",
        status_code=400,
        details={"source": kind, "raws": list(raws), "versions": list(versions)},
    )


def _get_all(mapping: Mapping[str, str], key: str) -> list[str]:
    """Return every value stored under ``key``.

    Multi-value mappings (Starlette ``Headers`` / ``QueryParams``) expose
    ``getlist``; plain mappings yield at most one value.
    """
    getlist = getattr(mapping, "getlist", None)
    if callable(getlist):
        return [str(v) for v in getlist(key)]
    value = mapping.get(key)
    return [] if value is None else [str(value)]


def _extract_path(path: str) -> _SourceOutcome | None:
    match = _PATH_RE.match(path)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    return _SourceOutcome(kind="path", raws=(raw,))


def _extract_header(headers: Mapping[str, str]) -> _SourceOutcome | None:
    raws = _get_all(headers, _HEADER_NAME)
    if not raws:
        return None
    return _SourceOutcome(kind="header", raws=tuple(raws))


def _extract_query(query_params: Mapping[str, str]) -> _SourceOutcome | None:
    raws = _get_all(query_params, _QUERY_KEY)
    if not raws:
        return None
    return _SourceOutcome(kind="query", raws=tuple(raws))


def _iter_accept_params(accept: str) -> Iterable[tuple[str, str]]:
    """Yield (parameter, value) pairs from an Accept header value."""
    for media_range in accept.split(","):
        parts = [p.strip() for p in media_range.split(";")]
        for param in parts[1:]:
            if "=" not in param:
                continue
            key, value = param.split("=", 1)
            yield key.strip().lower(), value.strip().strip('"')


def _extract_media_type(headers: Mapping[str, str]) -> _SourceOutcome | None:
    raws: list[str] = []
    for accept in _get_all(headers, "accept"):
        for key, raw in _iter_accept_params(accept):
            if key == _MEDIA_PARAM:
                raws.append(raw)
    if not raws:
        return None
    return _SourceOutcome(kind="media_type", raws=tuple(raws))


def negotiate(
    path: str,
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
) -> NegotiationResult:
    """Negotiate a single version from the request parts.

    Raises :class:`VersionNegotiationError` (``version_required`` /
    ``version_invalid`` / ``version_conflict``) instead of guessing.
    """
    outcomes: list[_SourceOutcome] = []
    for outcome in (
        _extract_path(path),
        _extract_header(headers),
        _extract_media_type(headers),
        _extract_query(query_params),
    ):
        if outcome is not None:
            outcomes.append(outcome)

    if not outcomes:
        raise VersionNegotiationError(
            "no API version specified: provide one via URL path (/vN/...), "
            "the X-API-Version header, an Accept media type parameter, or ?version=",
            code="version_required",
            status_code=400,
        )

    # The highest-priority present source class decides; ``outcomes`` is
    # already in priority order.  Lower-priority sources can never fail
    # the request, no matter how malformed they are.
    deciding = outcomes[0]
    parsed = deciding.parsed
    if parsed is None:
        bad_raw = next(
            raw
            for raw in deciding.raws
            if not raw.strip() or not re.fullmatch(r"\d+", raw.strip())
        )
        raise _invalid_error(deciding.kind, bad_raw)
    distinct = sorted(set(parsed))
    if len(distinct) > 1:
        raise _conflict_error(deciding.kind, deciding.raws, distinct)

    version = distinct[0]
    # Record every source that yielded a well-formed single version, for
    # observability; malformed lower-priority sources are not listed.
    sources = tuple(
        VersionSource(kind=o.kind, raw=o.raws[0], version=o.distinct_versions[0])
        for o in outcomes
        if o.parsed is not None and len(o.distinct_versions) == 1
    )
    return NegotiationResult(version=version, sources=sources)
