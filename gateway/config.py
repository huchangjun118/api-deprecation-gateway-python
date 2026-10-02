"""Policy loading and validation.

A policy document is JSON with the following shape::

    {
      "min_deprecation_window_days": 30,
      "versions": [
        {
          "version": 2,
          "state": "deprecated",
          "introduced_at": "2024-01-01T00:00:00Z",
          "deprecated_at": "2024-06-01T00:00:00Z",
          "sunset_at": "2025-06-01T00:00:00Z",
          "successor": 3
        }
      ]
    }

Validation rejects, at load time:

* unknown lifecycle states, malformed or naive timestamps;
* metadata contradictions (e.g. ``state=offline`` without ``sunset_at``,
  ``state=available`` carrying deprecation dates);
* deprecation windows shorter than ``min_deprecation_window_days``;
* successors pointing at missing versions or at the version itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import PolicyError
from .policy import LifecycleState, Policy, VersionConfig

__all__ = ["parse_policy", "load_policy_file", "default_policy"]

_WINDOW_DAYS_KEY = "min_deprecation_window_days"
_VERSIONS_KEY = "versions"


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field}: expected an ISO-8601 timestamp string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError(f"{field}: invalid timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        raise PolicyError(
            f"{field}: timestamp {value!r} is timezone-naive; "
            "include an explicit offset (e.g. 'Z' or '+00:00')"
        )
    return parsed.astimezone(timezone.utc)


def _parse_window(value: Any) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{_WINDOW_DAYS_KEY}: expected a number of days, got {value!r}")
    if value <= 0:
        raise PolicyError(f"{_WINDOW_DAYS_KEY}: must be positive, got {value}")
    return timedelta(days=float(value))


def _parse_state(value: Any, version: int) -> LifecycleState:
    if not isinstance(value, str):
        raise PolicyError(f"version {version}: state must be a string, got {value!r}")
    try:
        return LifecycleState(value)
    except ValueError:
        valid = ", ".join(s.value for s in LifecycleState)
        raise PolicyError(
            f"version {version}: unknown lifecycle state {value!r}; expected one of: {valid}"
        ) from None


def _validate_state_metadata(
    cfg: VersionConfig,
    now: datetime,
    min_window: timedelta,
) -> None:
    state = cfg.state
    v = cfg.version

    if state is LifecycleState.AVAILABLE:
        if cfg.sunset_at is not None:
            raise PolicyError(
                f"version {v}: state 'available' contradicts 'sunset_at' "
                f"{cfg.sunset_at.isoformat()} (use 'planned_offline' or 'deprecated')"
            )
        if cfg.deprecated_at is not None and cfg.deprecated_at <= now:
            raise PolicyError(
                f"version {v}: state 'available' contradicts 'deprecated_at' "
                f"{cfg.deprecated_at.isoformat()} which is already in effect"
            )

    elif state is LifecycleState.PLANNED_OFFLINE:
        if cfg.sunset_at is None:
            raise PolicyError(
                f"version {v}: state 'planned_offline' requires 'sunset_at'"
            )
        if cfg.deprecated_at is not None and cfg.deprecated_at <= now:
            raise PolicyError(
                f"version {v}: state 'planned_offline' contradicts 'deprecated_at' "
                f"{cfg.deprecated_at.isoformat()} which is already in the past"
            )

    elif state is LifecycleState.DEPRECATED:
        if cfg.deprecated_at is None:
            raise PolicyError(
                f"version {v}: state 'deprecated' requires 'deprecated_at'"
            )
        if cfg.deprecated_at > now:
            raise PolicyError(
                f"version {v}: state 'deprecated' contradicts 'deprecated_at' "
                f"{cfg.deprecated_at.isoformat()} which is in the future"
            )
        if cfg.sunset_at is None:
            raise PolicyError(
                f"version {v}: state 'deprecated' requires 'sunset_at' "
                "(a deprecated version must have a planned removal date)"
            )
        if now >= cfg.sunset_at:
            raise PolicyError(
                f"version {v}: state 'deprecated' contradicts 'sunset_at' "
                f"{cfg.sunset_at.isoformat()} which has already passed; use state 'offline'"
            )

    elif state is LifecycleState.OFFLINE:
        if cfg.sunset_at is None:
            raise PolicyError(
                f"version {v}: state 'offline' requires 'sunset_at'"
            )
        if cfg.sunset_at > now:
            raise PolicyError(
                f"version {v}: state 'offline' contradicts 'sunset_at' "
                f"{cfg.sunset_at.isoformat()} which is in the future"
            )

    # Chronology: introduced <= deprecated < sunset.
    if cfg.deprecated_at is not None and cfg.deprecated_at < cfg.introduced_at:
        raise PolicyError(
            f"version {v}: 'deprecated_at' precedes 'introduced_at'"
        )
    if cfg.sunset_at is not None:
        reference = cfg.deprecated_at or cfg.introduced_at
        if cfg.sunset_at <= reference:
            raise PolicyError(
                f"version {v}: 'sunset_at' must be after "
                f"{'deprecated_at' if cfg.deprecated_at else 'introduced_at'}"
            )

    # Minimum deprecation window.
    if cfg.deprecated_at is not None and cfg.sunset_at is not None:
        window = cfg.sunset_at - cfg.deprecated_at
        if window < min_window:
            raise PolicyError(
                f"version {v}: deprecation window {window.days}d "
                f"({cfg.deprecated_at.date()} -> {cfg.sunset_at.date()}) is shorter than "
                f"the required minimum of {min_window.days}d"
            )


def parse_policy(document: dict[str, Any], *, now: datetime, source: str = "document") -> Policy:
    """Parse and validate a policy document.

    Raises :class:`PolicyError` on any contradiction.  ``now`` is injected so
    load-time state checks are deterministic under tests.
    """
    if not isinstance(document, dict):
        raise PolicyError("policy document must be a JSON object")

    raw_versions = document.get(_VERSIONS_KEY)
    if not isinstance(raw_versions, list) or not raw_versions:
        raise PolicyError(f"'{_VERSIONS_KEY}' must be a non-empty list")

    min_window = _parse_window(document.get(_WINDOW_DAYS_KEY, 30))

    configs: dict[int, VersionConfig] = {}
    for index, raw in enumerate(raw_versions):
        if not isinstance(raw, dict):
            raise PolicyError(f"versions[{index}]: expected an object")
        raw_version = raw.get("version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise PolicyError(f"versions[{index}]: 'version' must be an integer, got {raw_version!r}")
        version = raw_version
        if version < 1:
            raise PolicyError(f"versions[{index}]: 'version' must be a positive integer")
        if version in configs:
            raise PolicyError(f"version {version}: duplicate version entry")

        state = _parse_state(raw.get("state"), version)
        introduced_at = _parse_timestamp(raw.get("introduced_at"), f"version {version}.introduced_at")
        deprecated_at = (
            _parse_timestamp(raw["deprecated_at"], f"version {version}.deprecated_at")
            if raw.get("deprecated_at") is not None
            else None
        )
        sunset_at = (
            _parse_timestamp(raw["sunset_at"], f"version {version}.sunset_at")
            if raw.get("sunset_at") is not None
            else None
        )
        successor = raw.get("successor")
        if successor is not None:
            if isinstance(successor, bool) or not isinstance(successor, int):
                raise PolicyError(f"version {version}: 'successor' must be an integer, got {successor!r}")
            if successor == version:
                raise PolicyError(f"version {version}: cannot succeed itself")

        cfg = VersionConfig(
            version=version,
            state=state,
            introduced_at=introduced_at,
            deprecated_at=deprecated_at,
            sunset_at=sunset_at,
            successor=successor,
        )
        _validate_state_metadata(cfg, now, min_window)
        configs[version] = cfg

    for cfg in configs.values():
        if cfg.successor is not None and cfg.successor not in configs:
            raise PolicyError(
                f"version {cfg.version}: successor {cfg.successor} is not present in the policy"
            )

    return Policy(versions=configs, min_deprecation_window=min_window, source=source)


def load_policy_file(path: str | Path, *, now: datetime) -> Policy:
    """Load a policy from a JSON file.  Raises :class:`PolicyError` / OSError."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path}: invalid JSON: {exc}") from exc
    return parse_policy(document, now=now, source=str(path))


def default_policy(*, now: datetime) -> Policy:
    """Build the built-in default policy, consistent at ``now``."""
    return parse_policy(
        {
            _WINDOW_DAYS_KEY: 30,
            _VERSIONS_KEY: [
                {
                    "version": 1,
                    "state": "offline",
                    "introduced_at": (now - timedelta(days=1000)).isoformat(),
                    "deprecated_at": (now - timedelta(days=900)).isoformat(),
                    "sunset_at": (now - timedelta(days=800)).isoformat(),
                    "successor": 2,
                },
                {
                    "version": 2,
                    "state": "deprecated",
                    "introduced_at": (now - timedelta(days=700)).isoformat(),
                    "deprecated_at": (now - timedelta(days=100)).isoformat(),
                    "sunset_at": (now + timedelta(days=265)).isoformat(),
                    "successor": 3,
                },
                {
                    "version": 3,
                    "state": "available",
                    "introduced_at": (now - timedelta(days=200)).isoformat(),
                },
            ],
        },
        now=now,
        source="default",
    )
