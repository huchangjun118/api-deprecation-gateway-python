"""ASGI middleware integrating version negotiation into FastAPI/Starlette.

Behaviour
---------
* Every non-exempt request is resolved to exactly one version before the
  application is called.
* Resolution failures return a JSON error with a distinct
  ``error.code`` (``version_required``, ``malformed_version``,
  ``ambiguous_version``, ``unknown_version``, ``version_not_introduced``
  or ``version_retired``). Retirement responses (HTTP 410) carry
  ``Deprecation`` / ``Sunset`` / ``Link`` / successor headers and a
  migration pointer in the body.
* Successful responses served by a deprecated or planned-retirement
  version get the deprecation hint headers added. The response *body is
  streamed through byte-for-byte unchanged*, so clients parsing the
  existing payload cannot break.
* The active :class:`Policy` is fetched once per request and then only
  read, so reloads never wait on slow upstream responses.
"""

from __future__ import annotations

import json

from .clock import Clock, SystemClock
from .errors import VersionNegotiationError
from .headers import build_deprecation_headers, build_retirement_headers
from .metrics import VersionMetrics
from .models import Policy, VersionState
from .negotiation import RequestView
from .resolver import resolve
from .store import PolicyStore

_DEPRECATED_STATES = (
    VersionState.DEPRECATED,
    VersionState.SCHEDULED_RETIREMENT,
)
# Headers we always (re)generate ourselves.
_OWNED_HEADERS = frozenset({
    "deprecation", "sunset", "link", "x-api-successor-version",
    "x-deprecation-date", "content-length",
})


class VersionGatewayMiddleware:
    def __init__(self, app, store: PolicyStore, clock: Clock | None = None,
                 metrics: VersionMetrics | None = None,
                 exempt_paths: tuple[str, ...] = ("/docs", "/redoc",
                                                 "/openapi.json")) -> None:
        self.app = app
        self.store = store
        self.clock = clock if clock is not None else SystemClock()
        self.metrics = metrics if metrics is not None else VersionMetrics()
        self.exempt_paths = exempt_paths

    # ------------------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "/")
        if path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        request = self._view_from_scope(scope)
        try:
            decision = resolve(request, self.store, self.clock)
        except VersionNegotiationError as exc:
            self.metrics.record_rejection(
                getattr(exc, "version", None))
            await self._send_error(send, exc, request)
            return
        except Exception:  # pragma: no cover - defensive
            await self.app(scope, receive, send)
            return

        self.metrics.record_decision(decision)
        extra = build_deprecation_headers(
            decision.info, decision.at,
            self.store.require(), request.path,
        ) if decision.effective_state in _DEPRECATED_STATES else {}

        if not extra:
            await self.app(scope, receive, send)
            return

        await self._wrap_response(scope, receive, send, extra)

    # ------------------------------------------------------------------
    @staticmethod
    def _view_from_scope(scope) -> RequestView:
        headers = {}
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin-1")
            value = raw_value.decode("latin-1")
            # Multiple header lines with the same name: join per RFC 9110.
            headers[name] = f"{headers[name]}, {value}" if name in headers else value
        query: dict[str, str] = {}
        from urllib.parse import parse_qsl

        for key, value in parse_qsl(scope.get("query_string", b"").decode("latin-1"),
                                    keep_blank_values=True):
            query.setdefault(key, value)
        return RequestView(
            method=scope.get("method", "GET"),
            path=scope.get("path", "/"),
            headers=headers,
            query_params=query,
        )

    async def _wrap_response(self, scope, receive, send,
                             extra: dict[str, str]) -> None:
        """Call the app and inject headers, preserving the body bytes."""
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                merged = [(name, value) for name, value in message.get("headers", [])
                          if name.decode("latin-1").lower() not in _OWNED_HEADERS]
                for key, value in extra.items():
                    merged.append(
                        (key.lower().encode("latin-1"),
                         value.encode("latin-1")))
                message = dict(message)
                message["headers"] = merged
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _send_error(self, send, exc: VersionNegotiationError,
                          request: RequestView) -> None:
        policy = self.store.get()
        headers: dict[str, str] = {}
        body: dict = {
            "error": {
                "code": exc.error_code,
                "message": str(exc),
            }
        }
        if exc.available_versions is not None:
            body["error"]["available_versions"] = exc.available_versions
        if exc.successor is not None:
            body["error"]["successor_version"] = exc.successor
        if exc.successor_url is not None:
            body["error"]["successor_url"] = exc.successor_url

        if exc.error_code == "version_retired" and policy is not None:
            retired = (policy.get(exc.version)
                       if exc.version is not None else None)
            if retired is not None:
                headers = build_retirement_headers(
                    retired, self.store.observed_time(self.clock),
                    policy, request.path)

        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        sent_headers = [(b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode("ascii"))]
        for key, value in headers.items():
            sent_headers.append((key.lower().encode("latin-1"),
                                 value.encode("latin-1")))
        await send({"type": "http.response.start",
                    "status": exc.status_code,
                    "headers": sent_headers})
        await send({"type": "http.response.body", "body": payload})


def install(app, store_or_policy, clock: Clock | None = None,
            metrics: VersionMetrics | None = None,
            exempt_paths: tuple[str, ...] = ("/docs", "/redoc",
                                            "/openapi.json")
            ) -> tuple[PolicyStore, VersionMetrics]:
    """Attach the gateway to an ASGI/Starlette/FastAPI ``app``.

    Accepts either a ready :class:`PolicyStore` or a :class:`Policy`.
    Returns the store and metrics so callers can reload and inspect them.
    """
    if isinstance(store_or_policy, PolicyStore):
        store = store_or_policy
    else:
        store = PolicyStore(store_or_policy, clock=clock)
    metrics = metrics if metrics is not None else VersionMetrics()
    app.add_middleware(VersionGatewayMiddleware, store=store,
                       clock=clock, metrics=metrics,
                       exempt_paths=exempt_paths)
    return store, metrics
