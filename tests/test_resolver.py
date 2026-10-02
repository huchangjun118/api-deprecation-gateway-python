"""Deterministic version decisions, boundary instants and rejections."""

from __future__ import annotations

from datetime import timedelta

import pytest

from version_gateway import Policy, VersionInfo, VersionState
from version_gateway.negotiation import RequestView
from version_gateway.resolver import resolve

from .conftest import DEP, INTRO_V2, SUN, T0, make_policy, v1, v2


def req(path="/", headers=None, query=None):
    return RequestView("GET", path, headers or {}, query or {})


def test_default_version_when_no_source(store, clock):
    decision = resolve(req(), store, clock)
    assert decision.version == "2" and decision.source == "default"


def test_no_source_without_default_is_rejected(clock):
    from version_gateway import PolicyStore
    from version_gateway.errors import VersionNegotiationError

    # v2 is introduced late, so at T0 only v1 is servable; at INTRO_V2 v1
    # has already retired, leaving only v2.
    policy = Policy(versions={"1": v1(), "2": v2(introduced_at=INTRO_V2)})
    store = PolicyStore(policy, clock=clock)
    clock.set(INTRO_V2)
    with pytest.raises(VersionNegotiationError) as exc:
        resolve(req(), store, clock)
    assert exc.value.error_code == "version_required"
    assert exc.value.available_versions == ["2"]


def test_unknown_version_rejected(store, clock):
    from version_gateway.errors import VersionNegotiationError

    clock.set(INTRO_V2)
    with pytest.raises(VersionNegotiationError) as exc:
        resolve(req(headers={"X-Api-Version": "77"}), store, clock)
    assert exc.value.error_code == "unknown_version"
    assert exc.value.status_code == 404
    assert exc.value.version == "77"


def test_not_introduced_version_rejected(clock):
    from version_gateway import PolicyStore
    from version_gateway.errors import VersionNegotiationError

    policy = make_policy(versions={"1": v1(), "2": v2(introduced_at=INTRO_V2)})
    store = PolicyStore(policy, clock=clock)
    with pytest.raises(VersionNegotiationError) as exc:
        resolve(req(headers={"X-Api-Version": "2"}), store, clock)
    assert exc.value.error_code == "version_not_introduced"


def test_before_deprecation_version_is_available(store, clock):
    decision = resolve(req(path="/v1/x"), store, clock)
    assert decision.version == "1"
    assert decision.effective_state is VersionState.AVAILABLE


def test_exactly_deprecation_instant_is_deprecated(store, clock):
    clock.set(DEP)
    decision = resolve(req(path="/v1/x"), store, clock)
    assert decision.effective_state is VersionState.SCHEDULED_RETIREMENT


def test_between_deprecation_and_sunset_stays_servable(store, clock):
    clock.set(SUN - timedelta(seconds=1))
    decision = resolve(req(path="/v1/x"), store, clock)
    assert decision.effective_state is VersionState.SCHEDULED_RETIREMENT


def test_exactly_sunset_instant_rejects(store, clock):
    from version_gateway.errors import VersionNegotiationError

    clock.set(SUN)
    with pytest.raises(VersionNegotiationError) as exc:
        resolve(req(path="/v1/x"), store, clock)
    assert exc.value.error_code == "version_retired"
    assert exc.value.status_code == 410
    assert exc.value.version == "1"
    assert exc.value.successor == "2"
    assert exc.value.successor_url == "https://api.example.com/v2/"


def test_retired_after_sunset_rejected_from_every_source(store, clock):
    from version_gateway.errors import VersionNegotiationError

    clock.set(SUN)
    for request in (
        req(path="/v1/x"),
        req(headers={"X-Api-Version": "1"}),
        req(query={"api_version": "1"}),
        req(headers={"Accept": "application/json; version=1"}),
    ):
        with pytest.raises(VersionNegotiationError, match="retired"):
            resolve(request, store, clock)


def test_clock_rollback_after_sunset_keeps_retired(store, clock):
    from version_gateway.errors import VersionNegotiationError

    clock.set(SUN + timedelta(days=2))
    with pytest.raises(VersionNegotiationError):
        resolve(req(path="/v1/x"), store, clock)
    clock.set(T0)
    with pytest.raises(VersionNegotiationError) as exc:
        resolve(req(path="/v1/x"), store, clock)
    assert exc.value.error_code == "version_retired"


def test_deterministic_decisions_for_identical_sequences(store, clock):
    from version_gateway import FixedClock, PolicyStore

    def run():
        local = PolicyStore(make_policy(), clock=FixedClock(T0))
        c = FixedClock(T0)
        out = []
        for instant, request in (
            (T0, req(path="/v1/x")),
            (DEP, req(headers={"X-Api-Version": "1"})),
            (SUN, req(query={"api_version": "1"})),
        ):
            c.set(instant)
            try:
                d = resolve(request, local, c)
                out.append((d.version, d.effective_state.value, d.source))
            except Exception as exc:  # noqa: BLE001
                out.append(("error", exc.error_code, "none"))
        return out

    assert run() == run()
