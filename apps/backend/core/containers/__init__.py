"""
Per-user OpenClaw container orchestration (ECS Fargate).

Each subscriber gets a dedicated ECS Service running an OpenClaw gateway
with a per-user EFS access point for data isolation. Agent workspaces
and openclaw.json configs live on EFS, and Cloud Map handles service
discovery for routing.
"""

import logging
from typing import Optional

from core.containers.ecs_manager import EcsManager, EcsManagerError
from core.containers.workspace import Workspace, WorkspaceError, get_workspace

logger = logging.getLogger(__name__)

_ecs_manager: Optional[EcsManager] = None


def get_ecs_manager() -> EcsManager:
    """Get the ECS manager singleton."""
    global _ecs_manager
    if _ecs_manager is None:
        _ecs_manager = EcsManager()
    return _ecs_manager


__all__ = [
    "EcsManager",
    "EcsManagerError",
    "Workspace",
    "WorkspaceError",
    "get_ecs_manager",
    "get_workspace",
]
