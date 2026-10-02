"""Load-time validation: contradictory metadata is rejected before serving."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from version_gateway import Policy, VersionInfo, VersionState
from version_gateway.errors import PolicyValidationError
from version_gateway.lifecycle import effective_state, validate_policy

from .conftest import DEP, SUN, T0, v1, v2


def policy_with(**overrides):
    base = dict(versions={"1": v1(), "2": v2()},
                minimum_deprecation_window=timedelta(days=30))
    base.update(overrides)
    return Policy(**base)


def test_valid_policy_loads():
    validate_policy(policy_with())


def test_empty_versions_rejected():
    with pytest.raises(PolicyValidationError):
        validate_policy(Policy(versions={}))


def test_naive_timestamp_rejected():
    with pytest.raises(PolicyValidationError):
        validate_policy(Policy(versions={
            "2": VersionInfo("2", VersionState.AVAILABLE, datetime(2026, 1, 1))}))


def test_available_with_deprecated_at_contradiction():
    bad = VersionInfo("2", VersionState.AVAILABLE, T0, deprecated_at=DEP)
    with pytest.raises(PolicyValidationError, match="AVAILABLE"):
        validate_policy(Policy(versions={"2": bad}))


def test_available_with_sunset_contradiction():
    bad = VersionInfo("2", VersionState.AVAILABLE, T0, sunset_at=SUN)
    with pytest.raises(PolicyValidationError):
        validate_policy(Policy(versions={"2": bad}))


def test_deprecated_requires_deprecated_at_and_successor():
    with pytest.raises(PolicyValidationError, match="deprecated_at"):
        validate_policy(Policy(versions={
            "2": VersionInfo("2", VersionState.DEPRECATED, T0, successor="3"),
            "3": VersionInfo("3", VersionState.AVAILABLE, T0)}))
    with pytest.raises(PolicyValidationError, match="successor"):
        validate_policy(Policy(versions={
            "2": VersionInfo("2", VersionState.DEPRECATED, T0,
                             deprecated_at=DEP)}))


def test_planned_retirement_requires_sunset():
    with pytest.raises(PolicyValidationError, match="sunset_at"):
        validate_policy(Policy(versions={
            "2": VersionInfo("2", VersionState.SCHEDULED_RETIREMENT, T0,
                             deprecated_at=DEP, successor="3"),
            "3": VersionInfo("3", VersionState.AVAILABLE, T0)}))


def test_sunset_before_deprecated_rejected():
    bad = VersionInfo("1", VersionState.SCHEDULED_RETIREMENT, T0,
                      deprecated_at=SUN, sunset_at=DEP, successor="2")
    with pytest.raises(PolicyValidationError, match="sunset_at"):
        validate_policy(Policy(versions={"1": bad, "2": v2()}))


def test_deprecated_before_introduced_rejected():
    bad = VersionInfo("1", VersionState.DEPRECATED,
                      introduced_at=DEP, deprecated_at=T0, successor="2")
    with pytest.raises(PolicyValidationError):
        validate_policy(Policy(versions={"1": bad, "2": v2()}))


def test_window_violation_rejected():
    # 29d 23h 59m 59s is shorter than the 30d minimum.
    sunset = DEP + timedelta(days=30) - timedelta(seconds=1)
    bad = VersionInfo("1", VersionState.DEPRECATED, T0, DEP, sunset, "2")
    with pytest.raises(PolicyValidationError, match="window"):
        validate_policy(policy_with(versions={"1": bad, "2": v2()}))


def test_window_exactly_at_minimum_accepted():
    sunset = DEP + timedelta(days=30)
    ok = VersionInfo("1", VersionState.DEPRECATED, T0, DEP, sunset, "2")
    validate_policy(policy_with(versions={"1": ok, "2": v2()}))


def test_unknown_successor_rejected():
    bad = VersionInfo("1", VersionState.DEPRECATED, T0, DEP, SUN, successor="9")
    with pytest.raises(PolicyValidationError, match="not a known version"):
        validate_policy(Policy(versions={"1": bad, "2": v2()}))


def test_self_successor_rejected():
    bad = VersionInfo("1", VersionState.DEPRECATED, T0, DEP, SUN, successor="1")
    with pytest.raises(PolicyValidationError, match="its own successor"):
        validate_policy(Policy(versions={"1": bad}))


def test_successor_pointing_at_retired_rejected():
    retired = VersionInfo("9", VersionState.RETIRED, T0, DEP, SUN, "2")
    bad = VersionInfo("1", VersionState.DEPRECATED, T0, DEP, SUN, "9")
    with pytest.raises(PolicyValidationError, match="successor"):
        validate_policy(Policy(versions={"1": bad, "2": v2(), "9": retired}))


def test_unknown_default_version_rejected():
    with pytest.raises(PolicyValidationError):
        validate_policy(policy_with(default_version="42"))


def test_bad_path_regex_rejected():
    with pytest.raises(PolicyValidationError):
        validate_policy(policy_with(version_path_regex="("))
    with pytest.raises(PolicyValidationError):
        validate_policy(policy_with(version_path_regex=r"/v(\d+)"))


def test_url_template_requires_placeholder():
    with pytest.raises(PolicyValidationError):
        validate_policy(policy_with(version_url_template="https://x/v1/"))


def test_effective_state_boundaries_are_inclusive():
    info = v1()
    assert effective_state(info, DEP - timedelta(seconds=1)) is VersionState.AVAILABLE
    assert effective_state(info, DEP) is VersionState.SCHEDULED_RETIREMENT
    assert effective_state(info, SUN - timedelta(seconds=1)) is VersionState.SCHEDULED_RETIREMENT
    assert effective_state(info, SUN) is VersionState.RETIRED


def test_deprecated_without_sunset_is_plain_deprecated():
    info = VersionInfo("1", VersionState.DEPRECATED, T0, DEP, None, "2")
    assert effective_state(info, DEP) is VersionState.DEPRECATED


def test_declared_retired_always_retired():
    info = VersionInfo("1", VersionState.RETIRED, T0, DEP, SUN, "2")
    assert effective_state(info, T0 - timedelta(days=99)) is VersionState.RETIRED


def test_timezone_aware_comparison_equivalent_instants():
    # Same instant expressed in different zones must decide identically.
    info = v1()
    utc_dep = DEP
    tokyo_dep = datetime(2026, 2, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    assert effective_state(info, utc_dep) == effective_state(info, tokyo_dep)
