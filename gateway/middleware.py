"""FastAPI middleware applying version negotiation + deprecation headers.

The middleware takes a single policy snapshot at request entry and uses it for
the whole request, so concurrent reloads can never produce a mixed decision.
"""

from __future__ import annotations

from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .errors import GatewayError, VersionNegotiationError
from .headers import gateway_headers, successor_link
from .manager import PolicyManager
from .policy import LifecycleState

__all__ = ["VersionGatewayMiddleware", "BYPASS_PREFIXES"]

# Paths that are not versioned API surface (health, gateway internals, docs).
BYPASS_PREFIXES: tuple[str, ...] = (
    "/",
    "/gateway",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _is_bypassed(path: str, prefixes: Iterable[str]) -> bool:
    for prefix in prefixes:
        if prefix == "/":
            if path == "/":
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class VersionGatewayMiddleware(BaseHTTPMiddleware):
    """Negotiate a version, enforce lifecycle, attach deprecation headers."""

    def __init__(
        self,
        app,
        manager: PolicyManager,
        *,
        bypass_prefixes: Iterable[str] = BYPASS_PREFIXES,
    ) -> None:
        super().__init__(app)
        self._manager = manager
        self._bypass_prefixes = tuple(bypass_prefixes)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if _is_bypassed(path, self._bypass_prefixes):
            return await call_next(request)

        # Single snapshot for the whole request.
        policy = self._manager.current()

        try:
            result = negotiate_request(request)
        except VersionNegotiationError as error:
            return error_response(error)

        version = result.version
        cfg = policy.get(version)
        if cfg is None:
            error = GatewayError(
                f"API version {version} is not available",
                code="version_unknown",
                status_code=404,
                details={"version": version},
            )
            self._manager.record_decision(version, LifecycleState.OFFLINE)
            return error_response(error)

        state = self._manager.effective_state(policy, version)
        self._manager.record_decision(version, state)

        if state is LifecycleState.OFFLINE:
            return retired_response(version, cfg.successor)

        request.state.gateway_version = version
        request.state.gateway_state = state

        response = await call_next(request)

        response.headers["X-API-Version"] = str(version)
        response.headers["X-API-Version-Source"] = result.source
        for key, value in gateway_headers(cfg, state).items():
            response.headers[key] = value
        return response


def negotiate_request(request: Request):
    """Run negotiation against a Starlette request."""
    from .resolver import negotiate

    return negotiate(
        request.url.path,
        request.headers,
        request.query_params,
    )


def error_response(error: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.to_body())


def retired_response(version: int, successor: int | None) -> JSONResponse:
    """410 Gone for an offline version, with migration guidance."""
    if successor is not None:
        guidance = (
            f"API version {version} has been retired; migrate to version {successor}"
        )
        details = {"version": version, "successor": successor}
    else:
        guidance = f"API version {version} has been retired; see /gateway/versions for supported versions"
        details = {"version": version, "successor": None}

    error = GatewayError(
        guidance,
        code="version_retired",
        status_code=410,
        details=details,
    )
    response = JSONResponse(status_code=410, content=error.to_body())
    if successor is not None:
        response.headers["Link"] = successor_link(successor)
    return response
