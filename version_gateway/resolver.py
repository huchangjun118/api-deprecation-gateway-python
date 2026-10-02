"""Resolve one request to exactly one version decision.

The resolver combines:

* raw version extraction (path/header/query/media-type precedence),
* the policy snapshot's default version,
* metadata-driven effective lifecycle state evaluated against the
  store's monotonic observed instant.

It never guesses: unknown versions, not-yet-introduced versions,
missing versions without a default, and retired versions each produce a
distinct error code. Retired requests additionally carry successor
guidance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock
from .errors import (
    no_version_announced,
    version_not_found,
    version_not_introduced,
    version_retired,
)
from .lifecycle import effective_state
from .models import Policy, VersionInfo, VersionState
from .negotiation import RequestView, extract_version
from .store import PolicyStore

SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class VersionDecision:
    version: str
    info: VersionInfo
    effective_state: VersionState
    source: str
    at: object  # timezone-aware datetime of the decision


def _available_list(policy: Policy, at) -> list[str]:
    """Versions currently servable at instant ``at`` (stable order)."""
    result = []
    for version in sorted(policy.versions):
        info = policy.versions[version]
        if effective_state(info, at) is not VersionState.RETIRED:
            result.append(version)
    return result


def _successor_url(info: VersionInfo, policy: Policy,
                   request: RequestView) -> str | None:
    if info.successor is None:
        return None
    if policy.version_url_template is not None:
        from urllib.parse import quote

        return policy.version_url_template.replace(
            "{version}", quote(info.successor, safe=""))
    import re

    match = re.search(policy.version_path_regex, request.path)
    if match is not None:
        start, end = match.span("version")
        return (request.path[:start] + info.successor + request.path[end:])
    return None


def resolve(request: RequestView, store: PolicyStore,
            clock: Clock | None = None) -> VersionDecision:
    policy = store.require()
    # Monotonic observed instant: rollback cannot resurrect retirements.
    at = store.observed_time(clock)

    extracted = extract_version(
        request,
        header_name=policy.header_name,
        query_param=policy.query_param,
        accept_version_param=policy.accept_version_param,
        version_path_regex=policy.version_path_regex,
    )

    if extracted is None:
        if policy.default_version is None:
            raise no_version_announced(_available_list(policy, at))
        version_id = policy.default_version
        source = SOURCE_DEFAULT
    else:
        version_id = extracted.value
        source = extracted.source

    info = policy.get(version_id)
    if info is None:
        raise version_not_found(version_id, _available_list(policy, at))

    if at < info.introduced_at:
        raise version_not_introduced(version_id)

    state = effective_state(info, at)
    if state is VersionState.RETIRED:
        raise version_retired(
            version_id,
            info.successor,
            _successor_url(info, policy, request),
        )

    return VersionDecision(
        version=version_id,
        info=info,
        effective_state=state,
        source=source,
        at=at,
    )
