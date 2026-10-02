"""Extract a raw version identifier from an HTTP request.

Four sources can express the client's desired version, ordered by
precedence (highest first):

1. request path           (configured regex, default ``/v<version>``)
2. request header         (default ``X-Api-Version``)
3. query parameter        (default ``api_version``)
4. media-type parameter   (``Accept: application/json; version=2``)

When several sources are present they *never* raise a conflict error:
the highest-precedence present source wins deterministically. Errors are
reserved for the cases where no single version can be determined:

* the winning source carries an empty/blank value  -> ``malformed_version``
* the ``Accept`` header lists two different versions -> ``ambiguous_version``

The value itself is an opaque token; whether it names a known version is
decided later by the resolver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ambiguous_versions, malformed_version

SOURCE_PATH = "path"
SOURCE_HEADER = "header"
SOURCE_QUERY = "query"
SOURCE_MEDIA_TYPE = "media-type"


@dataclass(frozen=True)
class RequestView:
    """Framework-neutral view of the request parts relevant to negotiation."""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        if name in self.headers:
            return self.headers[name]
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True)
class ExtractedVersion:
    value: str
    source: str


_MEDIA_SPLIT = re.compile(r",")
_PARAM_SPLIT = re.compile(r";")
_TOKEN = re.compile(r"^\s*([!#$%&'*+\-.^_`|~0-9A-Za-z]+)\s*=")


def _dequote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        # Minimal quoted-string handling: strip quotes and backslash escapes.
        body = value[1:-1]
        return re.sub(r"\\(.)", r"\1", body)
    return value


def parse_accept_versions(accept_header: str, param_name: str) -> list[str]:
    """Return distinct ``version`` parameter values found in an Accept header.

    Order of first appearance is preserved. Distinct values in the same
    header indicate ambiguity and are returned together so the caller can
    raise :class:`ambiguous_versions`.
    """
    found: list[str] = []
    target = param_name.lower()
    for media_range in _MEDIA_SPLIT.split(accept_header):
        parts = _PARAM_SPLIT.split(media_range)
        for parameter in parts[1:]:  # parts[0] is the media type itself
            match = _TOKEN.match(parameter)
            if match is None:
                continue
            if match.group(1).lower() != target:
                continue
            raw_value = parameter[match.end():]
            value = _dequote(raw_value)
            if value not in found:
                found.append(value)
    return found


def _check_blank(value: str, where: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise malformed_version(f"blank value in {where}")
    return stripped


def _from_path(path: str, pattern: str) -> str | None:
    match = re.search(pattern, path)
    if match is None:
        return None
    try:
        raw = match.group("version")
    except IndexError:  # pragma: no cover - validated at policy load
        return None
    if raw is None or not raw.strip():
        return None
    return _check_blank(raw, "request path")


def _from_accept(accept: str | None, param_name: str) -> str | None:
    if not accept:
        return None
    values = parse_accept_versions(accept, param_name)
    if not values:
        return None
    distinct = {v.strip() for v in values if v.strip()}
    if len(distinct) != 1:
        # Blank values mixed in are also ambiguous/malformed.
        if any(not v.strip() for v in values):
            raise malformed_version("blank value in Accept media-type parameter")
        raise ambiguous_versions(values)
    return _check_blank(next(iter(distinct)), "Accept media-type parameter")


def extract_version(request: RequestView, *, header_name: str,
                    query_param: str, accept_version_param: str,
                    version_path_regex: str) -> ExtractedVersion | None:
    """Extract the winning raw version, or ``None`` when no source is set.

    Sources are probed in precedence order; the first present source
    decides and lower-precedence sources are ignored entirely, so a
    request using the path never fails because of an unrelated header.
    """
    path_value = _from_path(request.path, version_path_regex)
    if path_value is not None:
        return ExtractedVersion(path_value, SOURCE_PATH)

    header_value = request.header(header_name)
    if header_value is not None:
        # An explicitly present but blank value is malformed rather than
        # silently ignored: falling through to another source would be
        # guessing the client's intent.
        return ExtractedVersion(_check_blank(header_value, "request header"),
                                SOURCE_HEADER)

    query_value = request.query_params.get(query_param)
    if query_value is not None:
        return ExtractedVersion(_check_blank(query_value, "query parameter"),
                                SOURCE_QUERY)

    accept_value = _from_accept(request.header("Accept"), accept_version_param)
    if accept_value is not None:
        return ExtractedVersion(accept_value, SOURCE_MEDIA_TYPE)

    return None
