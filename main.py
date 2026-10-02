"""Demo application with API version negotiation and deprecation lifecycle.

The original root endpoint (``GET /`` -> ``{"Hello": "World"}``) keeps
exactly the same behaviour and response body. The version gateway is
installed as middleware; run with ``python main.py``.
"""

from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI

from version_gateway import (
    FixedClock,
    Policy,
    PolicyStore,
    VersionInfo,
    VersionMetrics,
    VersionState,
)
from version_gateway.middleware import install

app = FastAPI(title="API Deprecation Gateway demo")


def _demo_policy() -> Policy:
    introduced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    deprecated = datetime(2026, 2, 1, tzinfo=timezone.utc)
    sunset = deprecated + timedelta(days=30)
    return Policy(
        versions={
            "1": VersionInfo(
                version="1",
                state=VersionState.DEPRECATED,
                introduced_at=introduced,
                deprecated_at=deprecated,
                sunset_at=sunset,
                successor="2",
            ),
            "2": VersionInfo(
                version="2",
                state=VersionState.AVAILABLE,
                introduced_at=introduced,
            ),
        },
        default_version="2",
        minimum_deprecation_window=timedelta(days=30),
        version_url_template="/v{version}/",
    )


store = PolicyStore(_demo_policy())
metrics = VersionMetrics()
install(app, store, metrics=metrics)


@app.get("/")
def read_root():
    # Existing public contract: unchanged body and status.
    return {"Hello": "World"}


@app.get("/items")
def read_items():
    # Version-neutral resource; the chosen version is visible through the
    # deprecation response headers / metrics rather than the body.
    return {"items": []}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
