"""API version negotiation and deprecation lifecycle gateway."""

from .clock import Clock, FixedClock, SystemClock
from .errors import (
    PolicyValidationError,
    VersionGatewayError,
    VersionNegotiationError,
)
from .lifecycle import effective_state, validate_policy
from .metrics import VersionMetrics
from .models import Policy, VersionInfo, VersionState
from .negotiation import RequestView, extract_version
from .resolver import VersionDecision, resolve
from .store import PolicyStore

__all__ = [
    "Clock",
    "FixedClock",
    "Policy",
    "PolicyStore",
    "PolicyValidationError",
    "RequestView",
    "SystemClock",
    "VersionDecision",
    "VersionGatewayError",
    "VersionInfo",
    "VersionMetrics",
    "VersionNegotiationError",
    "VersionState",
    "effective_state",
    "extract_version",
    "resolve",
    "validate_policy",
]
