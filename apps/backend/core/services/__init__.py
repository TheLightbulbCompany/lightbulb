"""
Core services for Isol8.

Services encapsulate business logic for agent management,
WebSocket connection state, and other domain-specific operations.
"""

from .connection_service import (
    ConnectionService,
    ConnectionServiceError,
)

__all__ = [
    # Connection Service (WebSocket state)
    "ConnectionService",
    "ConnectionServiceError",
]
