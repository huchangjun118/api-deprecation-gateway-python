"""Shared fixtures for the version-gateway test suite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from version_gateway import (
    FixedClock,
    Policy,
    PolicyStore,
    VersionInfo,
    VersionState,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
DEP = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
SUN = datetime(2026, 3, 3, 0, 0, 0, tzinfo=timezone.utc)  # exactly 30d window
INTRO_V2 = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)


def v1(state=VersionState.DEPRECATED, deprecated_at=DEP, sunset_at=SUN,
       successor="2", version="1"):
    return VersionInfo(
        version=version, state=state, introduced_at=T0,
        deprecated_at=deprecated_at, sunset_at=sunset_at, successor=successor)


def v2(state=VersionState.AVAILABLE, introduced_at=T0, **kwargs):
    return VersionInfo(version="2", state=state, introduced_at=introduced_at,
                       **kwargs)


def make_policy(**overrides):
    params = dict(
        versions={"1": v1(), "2": v2()},
        default_version="2",
        minimum_deprecation_window=timedelta(days=30),
        version_url_template="https://api.example.com/v{version}/",
    )
    params.update(overrides)
    return Policy(**params)


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def policy():
    return make_policy()


@pytest.fixture
def store(policy, clock):
    return PolicyStore(policy, clock=clock)
