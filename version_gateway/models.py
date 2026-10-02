"""Version lifecycle domain models.

A :class:`Policy` is the immutable, self-consistent snapshot of the
version-negotiation configuration used to serve requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class VersionState(str, Enum):
    """Lifecycle state of a single API version.

    AVAILABLE
        Fully supported. No deprecation hints are emitted.
    DEPRECATED
        Still served, but ``Deprecation`` / ``Sunset`` / ``Link`` headers
        are attached to point clients at the successor.
    SCHEDULED_RETIREMENT ("planned-retirement")
        Deprecated *and* the retirement instant is known and announced.
        Same response hints as DEPRECATED; distinction is metadata-level.
    RETIRED ("retired")
        Permanently offline. Every request is rejected, regardless of the
        current clock value or the provenance of the version identifier.
    """

    AVAILABLE = "available"
    DEPRECATED = "deprecated"
    SCHEDULED_RETIREMENT = "planned-retirement"
    RETIRED = "retired"


@dataclass(frozen=True)
class VersionInfo:
    """Metadata of one API version.

    All timestamps are timezone-aware instants (UTC recommended).
    ``introduced_at`` is required; the other instants are present only when
    the corresponding lifecycle milestone has been announced.
    """

    version: str
    state: VersionState
    introduced_at: datetime
    deprecated_at: datetime | None = None
    sunset_at: datetime | None = None
    successor: str | None = None


@dataclass(frozen=True)
class Policy:
    """An immutable, validated strategy snapshot.

    Attributes:
        versions: Map of version id -> :class:`VersionInfo`.
        default_version: Version used when the request carries no version
            identifier. May be ``None``, in which case version-less
            requests are rejected.
        minimum_deprecation_window: Required minimum interval between
            ``deprecated_at`` and ``sunset_at``.
        header_name: Request header carrying a version identifier.
        query_param: Query parameter carrying a version identifier.
        accept_version_param: Media-type parameter (in ``Accept``) carrying
            a version identifier, e.g. ``version`` in
            ``application/json; version=2``.
        version_path_regex: Regex with one capture group extracting a
            version identifier from the request path.
        version_url_template: Template building the successor URL,
            ``{version}`` is substituted. ``None`` means only the version
            id is announced (no successor ``Link``).
    """

    versions: dict[str, VersionInfo] = field(default_factory=dict)
    default_version: str | None = None
    minimum_deprecation_window: timedelta = timedelta(days=30)
    header_name: str = "X-Api-Version"
    query_param: str = "api_version"
    accept_version_param: str = "version"
    version_path_regex: str = r"/v(?P<version>\d+)"
    version_url_template: str | None = None

    def get(self, version: str) -> VersionInfo | None:
        return self.versions.get(version)
