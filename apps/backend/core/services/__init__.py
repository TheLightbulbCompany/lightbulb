"""
Core services for Isol8.

Services encapsulate business logic for agent management,
WebSocket connection state, and other domain-specific operations.

Keep this package initializer lazy: importing a narrow service module must not
force optional integration dependencies (for example boto3/botocore) to load.
"""

__all__ = [
    "ConnectionService",
    "ConnectionServiceError",
    "ManagementApiClient",
    "ManagementApiClientError",
]


def __getattr__(name: str):
    if name in {"ConnectionService", "ConnectionServiceError"}:
        from .connection_service import ConnectionService, ConnectionServiceError

        return {"ConnectionService": ConnectionService, "ConnectionServiceError": ConnectionServiceError}[name]
    if name in {"ManagementApiClient", "ManagementApiClientError"}:
        from .management_api_client import ManagementApiClient, ManagementApiClientError

        return {"ManagementApiClient": ManagementApiClient, "ManagementApiClientError": ManagementApiClientError}[name]
    raise AttributeError(name)
