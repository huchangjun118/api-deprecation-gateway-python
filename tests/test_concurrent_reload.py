"""Concurrency: reloads interleaved with requests never corrupt serving."""

from __future__ import annotations

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from version_gateway import FixedClock, PolicyStore, VersionMetrics
from version_gateway.middleware import install

from .conftest import DEP, make_policy


def test_many_reloads_while_serving_all_responses_consistent():
    clock = FixedClock(DEP)
    store = PolicyStore(make_policy(), clock=clock)
    metrics = VersionMetrics()
    app = FastAPI()
    install(app, store, clock=clock, metrics=metrics)

    @app.get("/items")
    def items():
        return {"ok": True}

    client = TestClient(app)
    stop = threading.Event()
    failures: list[str] = []

    def reloader():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                store.reload(make_policy(default_version=("1" if i % 2 else "2")))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"reload: {exc!r}")

    def requester():
        while not stop.is_set():
            try:
                # v1 deprecated at DEP -> 200 with deprecation headers;
                # default alternates between v1/v2 across reloads, but every
                # observed response must be internally self-consistent.
                resp = client.get("/items")
                if resp.status_code != 200:
                    failures.append(f"status {resp.status_code}")
                    continue
                body_ok = resp.json() == {"ok": True}
                if not body_ok:
                    failures.append("body mutated")
                has_hint = "Deprecation" in resp.headers
                # Only v1 requests can carry deprecation hints.
                if has_hint and resp.headers["X-Api-Successor-Version"] != "2":
                    failures.append("inconsistent successor")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"request: {exc!r}")

    threads = [threading.Thread(target=reloader) for _ in range(2)]
    threads += [threading.Thread(target=requester) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert failures == []
