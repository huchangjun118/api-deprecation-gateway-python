"""Typed errors raised while loading policy or negotiating a version."""

from __future__ import annotations


class VersionGatewayError(Exception):
    """Base class for all version-gateway errors."""

    error_code = "version_error"
    status_code = 400


class PolicyValidationError(VersionGatewayError):
    """A policy configuration is internally inconsistent.

    Raised at load time only; such a policy can never become the active
    snapshot.
    """

    error_code = "invalid_policy"
    status_code = 500


class VersionNegotiationError(VersionGatewayError):
    """A single request cannot be mapped to exactly one version."""

    def __init__(self, message: str, *, code: str, status_code: int = 400,
                 available_versions: list[str] | None = None,
                 successor: str | None = None,
                 successor_url: str | None = None,
                 version: str | None = None) -> None:
        super().__init__(message)
        self.error_code = code
        self.status_code = status_code
        self.available_versions = available_versions
        self.successor = successor
        self.successor_url = successor_url
        self.version = version


def malformed_version(detail: str) -> VersionNegotiationError:
    return VersionNegotiationError(
        f"Malformed version identifier: {detail}",
        code="malformed_version",
        status_code=400,
    )


def ambiguous_versions(values: list[str]) -> VersionNegotiationError:
    return VersionNegotiationError(
        "Ambiguous version identifiers: " + ", ".join(values),
        code="ambiguous_version",
        status_code=400,
    )


def version_not_found(version: str, available: list[str]) -> VersionNegotiationError:
    return VersionNegotiationError(
        f"Unknown API version: {version!r}",
        code="unknown_version",
        status_code=404,
        available_versions=available,
        version=version,
    )


def version_not_introduced(version: str) -> VersionNegotiationError:
    return VersionNegotiationError(
        f"API version {version!r} has not been introduced yet",
        code="version_not_introduced",
        status_code=404,
        version=version,
    )


def no_version_announced(available: list[str]) -> VersionNegotiationError:
    return VersionNegotiationError(
        "Request did not specify an API version and no default is configured",
        code="version_required",
        status_code=400,
        available_versions=available,
    )


def version_retired(version: str, successor: str | None,
                    successor_url: str | None) -> VersionNegotiationError:
    if successor:
        message = (f"API version {version!r} has been retired; "
                   f"migrate to version {successor!r}")
    else:
        message = f"API version {version!r} has been retired"
    return VersionNegotiationError(
        message,
        code="version_retired",
        status_code=410,
        successor=successor,
        successor_url=successor_url,
        version=version,
    )
