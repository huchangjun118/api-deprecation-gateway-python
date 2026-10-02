"""Policy loading, validation and effective lifecycle states.

A policy is *loaded* once and then treated as immutable. Loading rejects
every configuration where declared state and metadata contradict each
other, where timestamps are missing/naive/out of order, where the
deprecation window is too short, or where successor pointers are broken.

The *effective* state of a version at request time is derived from the
metadata and the request instant. Boundary instants are handled
inclusively: a request whose instant equals ``deprecated_at`` is already
deprecated, and one equal to ``sunset_at`` is already retired.
"""

from __future__ import annotations

import re
from datetime import datetime

from .errors import PolicyValidationError
from .models import Policy, VersionInfo, VersionState

_STATES_WITH_DEPRECATION = (
    VersionState.DEPRECATED,
    VersionState.SCHEDULED_RETIREMENT,
    VersionState.RETIRED,
)
_STATES_WITH_SUNSET = (
    VersionState.SCHEDULED_RETIREMENT,
    VersionState.RETIRED,
)


def ensure_aware(instant: datetime, field_name: str) -> datetime:
    """Reject naive timestamps at load time; never guess a timezone."""
    if instant.tzinfo is None:
        raise PolicyValidationError(
            f"{field_name} must be a timezone-aware datetime (got a naive value)"
        )
    return instant


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyValidationError(message)


def validate_version(info: VersionInfo, known_versions: set[str],
                     minimum_window_seconds: float) -> None:
    label = f"version {info.version!r}"

    _require(isinstance(info.version, str) and bool(info.version.strip()),
             f"{label}: version id must be a non-empty string")
    ensure_aware(info.introduced_at, f"{label}.introduced_at")

    deprecated = info.deprecated_at
    sunset = info.sunset_at

    # --- field presence must agree with the declared state -------------
    if info.state is VersionState.AVAILABLE:
        _require(deprecated is None,
                 f"{label}: AVAILABLE version must not set deprecated_at")
        _require(sunset is None,
                 f"{label}: AVAILABLE version must not set sunset_at")
        _require(info.successor is None,
                 f"{label}: AVAILABLE version must not declare a successor")

    if info.state in _STATES_WITH_DEPRECATION:
        _require(deprecated is not None,
                 f"{label}: {info.state.value} version requires deprecated_at")
        _require(isinstance(info.successor, str) and bool(info.successor.strip()),
                 f"{label}: {info.state.value} version requires a successor "
                 "so clients can be directed somewhere")
    if info.state in _STATES_WITH_SUNSET:
        _require(sunset is not None,
                 f"{label}: {info.state.value} version requires sunset_at")

    if deprecated is not None:
        ensure_aware(deprecated, f"{label}.deprecated_at")
        _require(not deprecated < info.introduced_at,
                 f"{label}: deprecated_at must not predate introduced_at")
    if sunset is not None:
        ensure_aware(sunset, f"{label}.sunset_at")
        _require(deprecated is not None,
                 f"{label}: sunset_at without deprecated_at is contradictory")
        _require(not sunset < deprecated,
                 f"{label}: sunset_at must not predate deprecated_at")

    # --- minimum deprecation window ------------------------------------
    if deprecated is not None and sunset is not None:
        window = (sunset - deprecated).total_seconds()
        _require(window >= minimum_window_seconds,
                 f"{label}: deprecation window of {window:.0f}s is shorter "
                 f"than the required {minimum_window_seconds:.0f}s")

    # --- successor integrity -------------------------------------------
    if info.successor is not None:
        _require(info.successor != info.version,
                 f"{label}: a version cannot be its own successor")
        _require(info.successor in known_versions,
                 f"{label}: successor {info.successor!r} is not a known version")


def validate_policy(policy: Policy) -> None:
    """Validate the whole policy snapshot; raise on first contradiction."""
    _require(isinstance(policy.versions, dict) and len(policy.versions) > 0,
             "policy must define at least one version")
    _require(policy.minimum_deprecation_window.total_seconds() >= 0,
             "minimum_deprecation_window must not be negative")
    _require(bool(policy.header_name.strip()),
             "header_name must be non-empty")
    _require(bool(policy.query_param.strip()),
             "query_param must be non-empty")
    _require(bool(policy.accept_version_param.strip()),
             "accept_version_param must be non-empty")
    try:
        compiled = re.compile(policy.version_path_regex)
    except re.error as exc:
        raise PolicyValidationError(
            f"invalid version_path_regex: {exc}") from exc
    _require("version" in compiled.groupindex,
             "version_path_regex must contain a 'version' capture group")
    if policy.version_url_template is not None:
        _require("{version}" in policy.version_url_template,
                 "version_url_template must contain a {version} placeholder")

    if policy.default_version is not None:
        _require(policy.default_version in policy.versions,
                 f"default_version {policy.default_version!r} is not defined")

    known = set(policy.versions.keys())
    window_seconds = policy.minimum_deprecation_window.total_seconds()
    for info in policy.versions.values():
        validate_version(info, known, window_seconds)

    # Successor guidance must point clients at a version they can use.
    for info in policy.versions.values():
        if info.successor is not None:
            target = policy.versions[info.successor]
            _require(target.state is not VersionState.RETIRED,
                     f"version {info.version!r}: successor "
                     f"{info.successor!r} is itself retired")


def effective_state(info: VersionInfo, now: datetime) -> VersionState:
    """Return the lifecycle state in effect at instant ``now``.

    Rules (boundary instants inclusive):

    * A version declared RETIRED is retired for every possible instant;
      clock changes can never resurrect it.
    * ``now >= sunset_at`` -> RETIRED.
    * ``now >= deprecated_at`` -> SCHEDULED_RETIREMENT when a sunset is
      announced, otherwise DEPRECATED.
    * Otherwise -> AVAILABLE (this includes a deprecation that has been
      announced but whose instant has not arrived).
    """
    if info.state is VersionState.RETIRED:
        return VersionState.RETIRED
    if info.sunset_at is not None and now >= info.sunset_at:
        return VersionState.RETIRED
    if info.deprecated_at is not None and now >= info.deprecated_at:
        if info.sunset_at is not None:
            return VersionState.SCHEDULED_RETIREMENT
        return VersionState.DEPRECATED
    return VersionState.AVAILABLE
