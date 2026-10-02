"""Gateway error types.

All negotiation / policy errors carry a machine-readable ``code`` so clients
can distinguish "missing version", "conflicting versions" and "retired
version" failures instead of guessing.
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """Base class for gateway errors.

    Attributes:
        code: machine readable error code (e.g. ``version_conflict``).
        status_code: suggested HTTP status code for the error.
        details: optional structured details for the error body.
    """

    code: str = "gateway_error"
    status_code: int = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.details:
            body["error"]["details"] = self.details
        return body


class PolicyError(ValueError):
    """Raised when a policy fails to load or validate.

    Policy errors are fatal for the *candidate* policy: a failed reload must
    never replace the currently active policy.
    """


class VersionNegotiationError(GatewayError):
    """Raised when a request cannot be mapped to a single version."""
