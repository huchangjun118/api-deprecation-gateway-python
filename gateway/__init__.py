"""API deprecation gateway: version negotiation and deprecation lifecycle management."""

from .clock import Clock, FakeClock, SystemClock
from .errors import GatewayError, PolicyError, VersionNegotiationError
from .manager import PolicyManager
from .middleware import VersionGatewayMiddleware
from .policy import LifecycleState, Policy, VersionConfig
from .resolver import NegotiationResult, negotiate
from .stats import VersionStats

__all__ = [
    "LifecycleState",
    "VersionConfig",
    "Policy",
    "PolicyError",
    "GatewayError",
    "VersionNegotiationError",
    "Clock",
    "SystemClock",
    "FakeClock",
    "PolicyManager",
    "VersionStats",
    "VersionGatewayMiddleware",
    "negotiate",
    "NegotiationResult",
]
