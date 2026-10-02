"""FastAPI application wired with the version deprecation gateway.

Run with ``uvicorn main:app`` (or ``python main.py``).  Set
``GATEWAY_POLICY_PATH`` to a policy JSON file to override the built-in default
policy; ``POST /gateway/reload`` hot-reloads that file (or a JSON document
posted to the endpoint) without restarting the process.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.clock import SystemClock
from gateway.config import default_policy
from gateway.manager import PolicyManager
from gateway.middleware import VersionGatewayMiddleware

POLICY_PATH_ENV = "GATEWAY_POLICY_PATH"

clock = SystemClock()
manager = PolicyManager(default_policy(now=clock.now()), clock=clock)

app = FastAPI(title="API Deprecation Gateway", version="1.0.0")
app.add_middleware(VersionGatewayMiddleware, manager=manager)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


@app.get("/v1/ping")
def v1_ping(request: Request) -> dict[str, Any]:
    return {"version": 1, "message": "pong"}


@app.get("/v2/ping")
def v2_ping(request: Request) -> dict[str, Any]:
    return {"version": 2, "message": "pong"}


@app.get("/v3/ping")
def v3_ping(request: Request) -> dict[str, Any]:
    return {"version": 3, "message": "pong"}


@app.get("/gateway/versions")
def gateway_versions() -> dict[str, Any]:
    policy = manager.current()
    versions = []
    for cfg in policy.sorted_versions():
        state = manager.effective_state(policy, cfg.version)
        versions.append(
            {
                "version": cfg.version,
                "state": state.value,
                "introduced_at": cfg.introduced_at.isoformat(),
                "deprecated_at": cfg.deprecated_at.isoformat() if cfg.deprecated_at else None,
                "sunset_at": cfg.sunset_at.isoformat() if cfg.sunset_at else None,
                "successor": cfg.successor,
            }
        )
    return {"versions": versions, "source": policy.source}


@app.get("/gateway/stats")
def gateway_stats() -> dict[str, Any]:
    return manager.stats.snapshot()


@app.post("/gateway/reload")
async def gateway_reload(request: Request) -> JSONResponse:
    """Hot-reload the policy.

    With a JSON body the body is treated as the new policy document; otherwise
    the file at ``GATEWAY_POLICY_PATH`` is reloaded.  On validation failure the
    active policy is left untouched and a 400 error is returned.
    """
    try:
        body = await request.json()
    except Exception:
        body = None

    try:
        if isinstance(body, dict) and "versions" in body:
            new_policy = manager.reload_document(body)
        else:
            path = os.environ.get(POLICY_PATH_ENV)
            if not path:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "no_policy_source",
                            "message": (
                                "post a policy document as JSON or set "
                                f"{POLICY_PATH_ENV} to a policy file path"
                            ),
                        }
                    },
                )
            new_policy = manager.reload_file(path)
    except Exception as exc:  # noqa: BLE001 - reload must never crash the app
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "reload_failed",
                    "message": str(exc),
                }
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "reloaded",
            "source": new_policy.source,
            "versions": [c.version for c in new_policy.sorted_versions()],
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
